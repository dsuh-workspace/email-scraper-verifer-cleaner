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

import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Set

import requests
from sqlalchemy.orm import sessionmaker

from app.db.database import engine
from app.db.create_tables import Business, Contact

Session = sessionmaker(bind=engine)

# TLD length {2,} — the old {2,4} bound rejected legitimate industry TLDs
# like .plumbing, .services, .contractors, .museum which show up on
# HVAC/plumbing sites.
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Exclude asset paths that occasionally regex-match but are never emails.
EXCLUDE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf",
    ".webp", ".css", ".js",
)

# Also exclude obvious CDN/tracking noise found in scraped HTML.
EXCLUDE_DOMAINS = (
    "sentry.io",
    "wixpress.com",
    "example.com",
    "domain.com",
)

# Paths to try in order. First hit that returns emails short-circuits.
# Homepage first because many small biz sites do drop a mailto on it.
CONTACT_PATHS = ("", "/contact", "/contact-us", "/about", "/about-us", "/team")

REQUEST_TIMEOUT_SEC = 7
MAX_WORKERS = 10
PER_HOST_DELAY_SEC = 0.75  # gap between requests hitting the same host

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
        lock = _host_locks.get(host)
        if lock is None:
            lock = threading.Lock()
            _host_locks[host] = lock
        return lock


def extract_emails_from_html(html_text: str) -> List[str]:
    """
    Extract unique lower-cased email addresses from an HTML blob, dropping
    obvious false positives (image filenames, CDN noise).
    """
    emails: Set[str] = set()
    for match in EMAIL_REGEX.findall(html_text):
        email = match.lower()
        if any(email.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
            continue
        if any(bad in email for bad in EXCLUDE_DOMAINS):
            continue
        emails.add(email)
    return sorted(emails)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def _crawl_business(url: str) -> List[str]:
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
                    allow_redirects=True,
                )
            except requests.RequestException:
                continue

            if resp.status_code != 200 or not resp.text:
                continue

            emails = extract_emails_from_html(resp.text)
            if emails:
                found.update(emails)
                # Homepage often has one address; contact pages usually
                # have all of them. Keep going until we've tried /contact.
                if path in ("/contact", "/contact-us"):
                    break

            time.sleep(PER_HOST_DELAY_SEC)

    return sorted(found)


def _persist_emails_for_business(session, biz: Business, emails: List[str]) -> int:
    """
    Add new Contact rows for each found email; remove phone-only placeholders
    once we have at least one real email for the business. Returns count added.
    """
    if not emails:
        return 0

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
            session.delete(placeholder)

    return added


def harvest_emails_from_websites() -> None:
    """
    Fan out across all businesses that have a website but no email contact yet.
    Persists results in the main thread — SQLAlchemy sessions are not thread-safe.
    """
    session = Session()
    try:
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
        pending = [b for b in businesses if b.id not in already_contacted]
        print(
            f"Checking {len(pending)} businesses for website email extraction "
            f"({len(businesses) - len(pending)} already have emails)."
        )

        emails_harvested = 0

        # Fan out crawls in worker threads. Persistence happens serially in
        # this thread as results come back.
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_to_biz = {
                pool.submit(_crawl_business, biz.website): biz
                for biz in pending
            }

            for i, fut in enumerate(as_completed(future_to_biz), start=1):
                biz = future_to_biz[fut]
                try:
                    emails = fut.result()
                except Exception as e:
                    print(f"  -> Crawl error for {biz.website}: {e}")
                    continue

                if emails:
                    print(f"[{i}/{len(pending)}] {biz.website} -> {', '.join(emails)}")
                    added = _persist_emails_for_business(session, biz, emails)
                    emails_harvested += added

                # Batch commit every 25 businesses so a crash doesn't lose it all.
                if i % 25 == 0:
                    session.commit()

        session.commit()
        print(
            f"Email harvesting completed. "
            f"Harvested {emails_harvested} unique email contacts."
        )
    except Exception as e:
        session.rollback()
        print(f"Error during email harvesting: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    harvest_emails_from_websites()
