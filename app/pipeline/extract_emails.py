"""
Website email harvester.

For each Business that has a website but no email contacts yet, fetch a
shortlist of paths (see CONTACT_PATHS), regex out email addresses, and
persist them as Contact rows.

Design notes
------------
- **Multi-page**: contact info almost never lives on the homepage alone —
  /contact, /contact-us, /about, and /team account for the majority of
  hits in the HVAC/plumbing space. The list also covers /privacy,
  /privacy-policy, /terms and /terms-of-service, which is where a site
  that publishes no address elsewhere usually leaks one. Those legal pages
  are also the main source of web-agency and webmaster addresses, which is
  why email_filters.py exists.
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
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

import requests
from email_validator import EmailNotValidError, validate_email

from app.pipeline.email_filters import (
    ASSET_EXTENSIONS,
    EXCLUDE_LOCALPARTS,
    JUNK_EMAIL_SUBSTRINGS,
    is_junk_email,
)
from app.proxy_utils import load_proxy_file, validate_proxy_url

logger = logging.getLogger(__name__)

# TLD length {2,} — the old {2,4} bound rejected legitimate industry TLDs
# like .plumbing, .services, .contractors, .museum which show up on
# HVAC/plumbing sites.
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Junk filters live in email_filters.py so the crawler, the ingest path, and
# the export gate all apply the same list (they used to keep three that
# drifted). Re-exported under the historical names because
# `scripts/analysis/export_cohort.py` and `scripts/consolidate_exports.py`
# import them from this module.
EXCLUDE_EXTENSIONS = ASSET_EXTENSIONS
EXCLUDE_DOMAINS = JUNK_EMAIL_SUBSTRINGS

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

# Resolved/unresolved verdicts per hostname. Bounded by the number of distinct
# domains in one harvest, so no eviction policy is needed.
_dns_cache: dict[str, bool] = {}
_dns_cache_guard = threading.Lock()


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
        if is_junk_email(candidate):
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


def _host_resolves(host: str) -> bool:
    """DNS-check a host before spending any proxy requests on it.

    Small-business domains lapse constantly, and a dead one used to cost ten
    proxied requests (one per CONTACT_PATHS entry) plus ten politeness sleeps
    before we gave up on it. Resolution happens from this machine, not through
    the proxy, so a miss here is free.

    Cached because the answer can't change within a run and `harvest` may see
    the same host on several businesses (franchises share domains).

    Only proves the name resolves — a parked domain resolves fine. The
    homepage short-circuit in `_crawl_business` catches those.
    """
    host = (host or "").split(":")[0].lower()
    if not host:
        return False

    with _dns_cache_guard:
        cached = _dns_cache.get(host)
    if cached is not None:
        return cached

    try:
        socket.getaddrinfo(host, None)
        resolved = True
    except socket.gaierror:
        resolved = False
    except Exception:  # noqa: BLE001
        # Anything unexpected: assume alive and let the fetch decide, rather
        # than silently skipping a business over a resolver quirk.
        resolved = True

    with _dns_cache_guard:
        _dns_cache[host] = resolved
    return resolved


def _crawl_business(url: str, proxies: Optional[dict[str, str]] = None) -> List[str]:
    """
    Fetch each path in CONTACT_PATHS in order, aggregating emails.

    Stops early only after /contact or /contact-us yields something — not at
    the first hit of any kind. A homepage address is usually one of several,
    while a contact page tends to carry the full set, so a homepage hit is
    worth continuing past. If neither contact path ever hits, all ten paths
    are fetched.

    Two cheap exits before that, both aimed at dead domains — which are the
    dominant cost here, not slow ones. As of 2026-08-07 they accounted for
    roughly 2,300 of ~2,500 failed proxy requests, because every dead host was
    tried ten times:

    1. The host must resolve at all (free, no proxy).
    2. If the *homepage* fails to connect, the remaining nine paths are
       skipped — they are on the same host, so they cannot do better. A
       non-200 is different and does not trigger this: plenty of sites 404
       their root while serving /contact fine.

    Serialized per-host via _host_lock so we don't hit the same server
    with parallel bursts.
    """
    url = _normalize_url(url)
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.netloc.lower()

    if not _host_resolves(host):
        logger.debug("Skipping %s — host does not resolve.", host)
        return []

    found: Set[str] = set()
    lock = _host_lock(host)
    with lock:
        for path_index, path in enumerate(CONTACT_PATHS):
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
                if path_index == 0:
                    # Homepage wouldn't connect. The other nine paths are the
                    # same host, so they'd each burn a proxy request, a 7s
                    # timeout and a politeness sleep to fail identically.
                    logger.debug("Skipping %s — homepage unreachable.", host)
                    return []
                continue
            finally:
                time.sleep(PER_HOST_DELAY_SEC)

    return sorted(found)


def _persist_emails_for_business(
    session, biz, emails: List[str], scrape_run_id: Optional[int] = None
) -> int:
    """
    Add new Contact rows for each found email; remove phone-only placeholders
    once we have at least one real email for the business. Returns count added.

    `scrape_run_id` is stamped onto new contacts as `first_scrape_run_id`.
    Without it, every crawl-discovered email lands with NULL provenance and
    contact-level lift tables silently undercount exactly the emails that
    matter most (see `harvest_emails_from_websites`).
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
            first_scrape_run_id=scrape_run_id,
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

    from sqlalchemy import func

    from app.db.database import engine
    from app.db.create_tables import Business, Contact, ExportHistory, ScrapeRun

    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        # Provenance for crawl-discovered emails. The harvest is not itself a
        # scrape run, so attribute its contacts to the newest run in the DB —
        # i.e. the cohort whose pipeline invocation produced them. Without this
        # every crawled email lands with first_scrape_run_id = NULL and drops
        # out of contact-level lift tables.
        harvest_run_id = session.query(func.max(ScrapeRun.id)).scalar()
        if harvest_run_id is None:
            logger.warning(
                "No scrape_runs rows found; crawl-discovered contacts will have "
                "NULL first_scrape_run_id and will not appear in lift tables."
            )

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
                    added = _persist_emails_for_business(
                        session, biz, emails, scrape_run_id=harvest_run_id
                    )
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