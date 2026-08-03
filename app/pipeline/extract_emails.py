"""
Website email harvester.

For each Business that has a website but no email contacts yet, fetch a
small shortlist of paths (homepage, /contact, /about, /team, ...), regex
out email addresses, and persist them as Contact rows.

Design notes
------------
- **Multi-page**: contact info almost never lives on the homepage alone —
  /contact, /contact-us, /about, and /team account for the majority of
  hits in the HVAC/plumbing space.
- **Concurrent**: crawling is I/O bound. We fan out per-business with a
  ThreadPoolExecutor, ~10 workers, so a run over 500 sites finishes in
  minutes instead of an hour.
- **Per-host politeness**: even though each Business is a distinct
  domain, some franchises share one — a small per-host lock prevents us
  from hammering the same server. Homepage + subpage fetches for one
  business go through this lock in sequence.
"""

import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

import requests
from email_validator import EmailNotValidError, validate_email

from app.proxy_utils import load_proxy_file, validate_proxy_url

logger = logging.getLogger(__name__)

# TLD length {2,} — the old {2,4} bound rejected legitimate industry TLDs
# like .plumbing, .services, .contractors, .museum which show up on
# HVAC/plumbing sites.
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Exclude asset paths that occasionally regex-match but are never emails.
EXCLUDE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf",
    ".webp", ".css", ".js",
)

# Substring match against the full email. Blocks CDN/tracking noise plus
# well-known template-site placeholders that regex-match but never
# resolve to real inboxes. Verifier catches most of these too, but
# pre-filtering saves API calls + keeps junk out of the DB.
EXCLUDE_DOMAINS = (
    # tracking / CDN / build-tool noise
    "sentry.io",
    "sentry-next.wixpress.com",
    "wixpress.com",
    "wix.com",
    "cloudflare.com",
    "cloudfront.net",
    "googleusercontent.com",
    "gstatic.com",
    # recurring non-business/support-site emails found on crawled pages
    "latofonts.com",
    "pixelspread.com",
    "rioradio.org",
    "imtresidential.com",
    "newapthome.com",
    "engrain.com",
    "santaclarita.gov",
    "2pointagency.com",
    "astigmatic.com",
    "cdn.",
    # documentation / spec placeholders
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "yourdomain.com",
    "your-domain.com",
    "mysite.com",
    "your-email.com",
    "youremail.com",
    "yoursite.com",
    "email.com",
    # frequent template-site junk seen in scraped SJ/SC data
    "gami.com",
    "test.com",
    "sample.com",
    # registrar / host placeholder shown by parked pages
    "godaddy.com",
    "sentry-cdn.com",
)

# Paths to try in order. First hit that returns emails short-circuits.
# Homepage first because many small biz sites do drop a mailto on it.
CONTACT_PATHS = (
    "",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/team",
    "/privacy-policy",
    "/privacy",
    "/terms",
    "/terms-of-service",
)

REQUEST_TIMEOUT_SEC = 7
MAX_WORKERS = 10
PER_HOST_DELAY_SEC = 0.75  # gap between requests hitting the same host

# --- crawl-attempt ledger tuning (review #R7) ------------------------------
# A crawl that finds no email leaves no trace in `contacts`, so without a
# ledger the same email-less domains are re-fetched on every depth iteration
# and every re-run. `businesses.last_crawled_at` / `.crawl_attempts` record
# the attempt; these two knobs decide when it's worth trying again.
#
# CRAWL_RETRY_AFTER_HOURS: cooldown before re-attempting a domain that
#   yielded nothing. Defaults to 720h (30 days) — long enough that the
#   depth loop within one run never re-crawls, short enough that a site
#   which later publishes an address gets picked up on a future run.
# CRAWL_MAX_ATTEMPTS: give up on a domain after this many consecutive
#   no-email attempts. 0 disables the cap (cooldown still applies).
DEFAULT_CRAWL_RETRY_AFTER_HOURS = 720
DEFAULT_CRAWL_MAX_ATTEMPTS = 3
ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Per-host lock registry — one lock per hostname, created lazily.
_host_locks: dict = {}
_host_locks_guard = threading.Lock()


def _host_lock(host: str) -> threading.Lock:
    """Return a per-host lock so parallel workers on the same domain serialize."""
    with _host_locks_guard:
        return _host_locks.setdefault(host, threading.Lock())


def extract_emails_from_html(html_text: str) -> List[str]:
    """
    Extract unique lower-cased email addresses from an HTML blob, dropping
    obvious false positives (image filenames, CDN noise).
    """
    emails: Set[str] = set()
    for match in EMAIL_REGEX.findall(html_text):
        candidate = match.lower()
        if any(candidate.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
            continue
        if any(bad in candidate for bad in EXCLUDE_DOMAINS):
            continue
        try:
            email = validate_email(candidate, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            continue
        emails.add(email)
    return sorted(emails)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r'^https?://', url, re.IGNORECASE):
        url = "http://" + url
    return url


def _build_crawler_proxies(disable_proxy: bool = False) -> Optional[dict[str, str]]:
    if disable_proxy:
        logger.info("Crawler proxies disabled for this run.")
        return None

    http_proxy = os.getenv("CRAWLER_HTTP_PROXY", "").strip()
    https_proxy = os.getenv("CRAWLER_HTTPS_PROXY", "").strip()
    fallback_proxy = os.getenv("CRAWLER_PROXY", "").strip()
    proxy_file = os.getenv("CRAWLER_PROXY_FILE", "").strip()

    def _validate(url: str) -> str:
        return validate_proxy_url(
            url,
            error_prefix="Crawler",
            allowed_schemes=ALLOWED_PROXY_SCHEMES,
            unsupported_message="Unsupported crawler proxy scheme",
        )

    file_proxy = None
    if proxy_file:
        import random
        file_proxies = load_proxy_file(proxy_file)
        if file_proxies:
            random.shuffle(file_proxies)
            file_proxy = _validate(file_proxies[0])

    proxies = {}
    if http_proxy:
        proxies["http"] = _validate(http_proxy)
    elif fallback_proxy:
        proxies["http"] = _validate(fallback_proxy)
    elif file_proxy:
        proxies["http"] = file_proxy

    if https_proxy:
        proxies["https"] = _validate(https_proxy)
    elif fallback_proxy:
        proxies["https"] = _validate(fallback_proxy)
    elif file_proxy:
        proxies["https"] = file_proxy

    return proxies or None


def _crawl_business(url: str, proxies: Optional[dict[str, str]] = None) -> List[str]:
    """
    Fetch each path in CONTACT_PATHS in order, aggregating emails.
    Short-circuits at the first page that yields at least one email.

    Serialized per-host via _host_lock so we don't hit the same server
    with parallel bursts.
    """
    url = _normalize_url(url)
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.netloc.lower()

    found: Set[str] = set()
    lock = _host_lock(host)
    with lock:
        for path in CONTACT_PATHS:
            target = base + path
            try:
                resp = requests.get(
                    target, headers=_HEADERS, timeout=REQUEST_TIMEOUT_SEC,
                    proxies=proxies,
                )
                if resp.status_code != 200 or not resp.text:
                    continue

                emails = extract_emails_from_html(resp.text)
                if emails:
                    found.update(emails)
                    # Homepage often has one address; contact pages usually
                    # have all of them. Keep going until we've tried /contact.
                    if path in ("/contact", "/contact-us"):
                        break
            except requests.RequestException:
                continue
            finally:
                time.sleep(PER_HOST_DELAY_SEC)

    return sorted(found)


def _persist_emails_for_business(session, biz, emails: List[str]) -> int:
    """
    Add new Contact rows for each found email; remove phone-only placeholders
    once we have at least one real email for the business. Returns count added.
    """
    if not emails:
        return 0

    # Imported here, not at module scope, to match the deferred-import
    # convention the rest of this module uses (importing app.db at module
    # level fires DATABASE_URL validation on every worker import).
    # These were previously only imported inside
    # harvest_emails_from_websites(), which does not put them in this
    # function's scope — so this raised NameError on the first crawl that
    # actually found an email.
    from app.db.create_tables import Contact, ExportHistory

    added = 0
    # Load existing contacts for this business ONCE (batch, not N+1).
    existing = (
        session.query(Contact)
        .filter(Contact.business_id == biz.id)
        .all()
    )
    existing_emails = {c.email for c in existing if c.email}
    placeholders = [c for c in existing if c.email is None]

    for email in emails:
        if email in existing_emails:
            continue
        session.add(Contact(
            business_id=biz.id,
            name="Info/Office",
            phone=biz.phone,
            title="General Contact",
            email=email,
            lead_status="Not Contacted",
        ))
        existing_emails.add(email)
        added += 1

    if added:
        for placeholder in placeholders:
            session.query(ExportHistory).filter(
                ExportHistory.contact_id == placeholder.id
            ).delete(synchronize_session=False)
            session.delete(placeholder)

    return added


def _crawl_retry_policy() -> tuple[int, int]:
    """Resolve (retry_after_hours, max_attempts) from env, with defaults.

    Invalid or negative values fall back to the defaults rather than
    disabling the ledger — a typo in a cron env shouldn't silently restore
    the re-crawl-everything behavior this exists to prevent. `0` for
    max_attempts is a legitimate value meaning "no give-up cap".
    """
    def _int_env(name: str, default: int, allow_zero: bool) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            logger.warning("%s=%r is not an integer; using %d.", name, raw, default)
            return default
        if value < 0 or (value == 0 and not allow_zero):
            logger.warning("%s=%d is out of range; using %d.", name, value, default)
            return default
        return value

    return (
        _int_env("CRAWL_RETRY_AFTER_HOURS", DEFAULT_CRAWL_RETRY_AFTER_HOURS,
                 allow_zero=False),
        _int_env("CRAWL_MAX_ATTEMPTS", DEFAULT_CRAWL_MAX_ATTEMPTS,
                 allow_zero=True),
    )


def _is_within_cooldown(last_crawled_at, cutoff: datetime) -> bool:
    """True if `last_crawled_at` is recent enough to skip this business.

    SQLite hands back naive datetimes even for values written as aware, so
    normalize before comparing — a naive/aware comparison raises TypeError.
    A null timestamp means never crawled, which is never in cooldown.
    """
    if last_crawled_at is None:
        return False
    if last_crawled_at.tzinfo is None:
        last_crawled_at = last_crawled_at.replace(tzinfo=timezone.utc)
    return last_crawled_at > cutoff


def _record_crawl_attempt(biz, attempted_at: datetime, *, found_email: bool) -> None:
    """Stamp the ledger for one crawl attempt.

    Finding an email resets the counter: the field tracks *consecutive*
    no-email attempts, so a site that starts publishing an address isn't
    left pinned at the give-up threshold by its past misses.
    """
    biz.last_crawled_at = attempted_at
    biz.crawl_attempts = 0 if found_email else (biz.crawl_attempts or 0) + 1


def harvest_emails_from_websites(disable_proxy: bool = False) -> None:
    """
    Fan out across all businesses that have a website but no email contact yet.
    Persists results in the main thread — SQLAlchemy sessions are not thread-safe.
    """
    from sqlalchemy.orm import sessionmaker

    from app.db.database import engine
    from app.db.create_tables import Business, Contact, ExportHistory

    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        crawler_proxies = _build_crawler_proxies(disable_proxy=disable_proxy)
        if crawler_proxies:
            logger.info("Crawler proxies enabled for %s.", ", ".join(sorted(crawler_proxies)))
        # Preload the set of business_ids that already have at least one
        # email contact — single SELECT vs one-per-business.
        already_contacted = {
            row[0]
            for row in session.query(Contact.business_id)
            .filter(Contact.email.isnot(None))
            .distinct()
        }

        businesses = (
            session.query(Business)
            .filter(Business.website.isnot(None))
            .all()
        )

        # Three-way split (review #R7). Businesses that already have an email
        # are done. The rest are only worth crawling if the ledger says we
        # haven't just tried them and haven't given up on them — otherwise
        # every depth iteration re-fetches the same email-less domains.
        retry_after_hours, max_attempts = _crawl_retry_policy()
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(hours=retry_after_hours)

        pending, cooling_down, exhausted = [], 0, 0
        for b in businesses:
            if b.id in already_contacted:
                continue
            if max_attempts and (b.crawl_attempts or 0) >= max_attempts:
                exhausted += 1
                continue
            if _is_within_cooldown(b.last_crawled_at, cooldown_cutoff):
                cooling_down += 1
                continue
            pending.append(b)

        logger.info(
            "Checking %d businesses for website email extraction "
            "(%d already have emails, %d in crawl cooldown, %d past %d "
            "no-email attempts).",
            len(pending),
            len(businesses) - len(pending) - cooling_down - exhausted,
            cooling_down,
            exhausted,
            max_attempts,
        )

        emails_harvested = 0
        attempted_at = datetime.now(timezone.utc)

        # Fan out crawls in worker threads. Persistence happens serially in
        # this thread as results come back.
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_to_biz = {
                pool.submit(_crawl_business, biz.website, crawler_proxies): biz
                for biz in pending
            }

            for i, fut in enumerate(as_completed(future_to_biz), start=1):
                biz = future_to_biz[fut]
                try:
                    emails = fut.result()
                except Exception as e:
                    logger.error(f"  -> Crawl error for {biz.website}: {e}")
                    # Still a spent attempt — a domain that reliably errors
                    # would otherwise be retried on every iteration forever,
                    # which is the same bug the ledger exists to fix.
                    _record_crawl_attempt(biz, attempted_at, found_email=False)
                    continue

                if emails:
                    logger.info(f"[{i}/{len(pending)}] {biz.website} -> {', '.join(emails)}")
                    added = _persist_emails_for_business(session, biz, emails)
                    emails_harvested += added

                _record_crawl_attempt(biz, attempted_at, found_email=bool(emails))

                # Batch commit every 25 businesses so a crash doesn't lose it all.
                if i % 25 == 0:
                    session.commit()

        session.commit()
        logger.info(
            "Email harvesting completed. Harvested %d unique email contacts.",
            emails_harvested,
        )
    except Exception as e:
        session.rollback()
        logger.error(f"Error during email harvesting: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    harvest_emails_from_websites()