"""
Raw-lead cleaner + deduper.

Takes unprocessed rows from raw_leads and promotes each to the canonical
businesses table (with contacts hanging off it). Deduplicates on:

  1. base domain (rotorooter.com, joe-plumber.com, ...) — strongest signal
  2. exact business_name + normalized phone — fallback when no website

Key changes vs the naive first pass:

- Filters raw_leads to processed_at IS NULL, then stamps each row after
  handling it. Prevents quadratic reprocessing when the scraper loop
  retries at a higher depth.
- Pre-loads every existing Business into a domain-keyed dict and a
  (name, phone)-keyed dict up front. Inner loop is O(1) hash lookups
  instead of two SELECT round-trips per raw row (was: ~2N queries for N
  raw leads → now: 2 queries total).
- Same batch treatment for existing contacts.
- Runs raw email strings through a real regex + splits on ,;\\s so we
  don't insert garbage into Contact.email.
"""

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Tuple

from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import RawLead, Business, Contact

Session = sessionmaker(bind=engine)

# Same regex as extract_emails.py so validation is consistent across the pipeline.
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

# Raw scraper output separates emails with any of these — treat them all.
_EMAIL_SPLIT_RE = re.compile(r'[,;\s]+')


def extract_domain(url_str):
    """
    Extract the base domain (e.g. 'rotorooter.com') from a website URL.
    """
    if not url_str:
        return None
    try:
        url_str = url_str.strip()
        if not url_str.startswith(('http://', 'https://')):
            url_str = 'http://' + url_str
        parsed = urllib.parse.urlparse(url_str)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        # Remove port if present
        if ':' in domain:
            domain = domain.split(':')[0]
        return domain or None
    except Exception:
        return None


def normalize_phone(phone_str):
    """
    Normalize to E.164-ish (+1XXXXXXXXXX). Falls back to the stripped
    original if the digit count doesn't look like a US number.
    """
    if not phone_str:
        return None
    phone_str = phone_str.strip()
    digits = re.sub(r'\D', '', phone_str)

    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"

    return phone_str


def _parse_and_validate_emails(raw_email_field: str):
    """
    Split a scraper email field (which can contain commas, semicolons,
    or whitespace between addresses) and drop anything that doesn't
    validate against EMAIL_REGEX.
    """
    if not raw_email_field:
        return []
    parts = _EMAIL_SPLIT_RE.split(raw_email_field.strip())
    valid = []
    seen = set()
    for part in parts:
        email = part.strip().lower().rstrip('.,;')
        if not email or email in seen:
            continue
        if EMAIL_REGEX.match(email):
            seen.add(email)
            valid.append(email)
    return valid


def process_and_deduplicate_leads() -> None:
    """
    Main entrypoint. Processes unprocessed raw leads and promotes to
    businesses + contacts, deduping in memory to avoid N+1 queries.
    """
    session = Session()
    try:
        # -- Load unprocessed raw leads (skip anything with processed_at set) --
        raw_leads = (
            session.query(RawLead)
            .filter(RawLead.processed_at.is_(None))
            .all()
        )
        print(f"Loaded {len(raw_leads)} unprocessed raw leads for processing.")

        # -- Pre-load existing businesses into fast lookup dicts (one SELECT total) --
        existing_by_domain: Dict[str, Business] = {}
        existing_by_name_phone: Dict[Tuple[str, str], Business] = {}
        for biz in session.query(Business).all():
            if biz.domain:
                existing_by_domain[biz.domain] = biz
            if biz.business_name and biz.phone:
                existing_by_name_phone[(biz.business_name, biz.phone)] = biz

        # -- Pre-load contact fingerprints so we don't re-add duplicates --
        # keyed by (business_id, email) and (business_id, phone)
        existing_emails: set = set(
            (row[0], row[1])
            for row in session.query(Contact.business_id, Contact.email)
            .filter(Contact.email.isnot(None))
        )
        existing_phones: set = set(
            (row[0], row[1])
            for row in session.query(Contact.business_id, Contact.phone)
            .filter(Contact.phone.isnot(None))
        )

        businesses_added = 0
        contacts_added = 0
        now = datetime.now(timezone.utc)

        for raw in raw_leads:
            cleaned_name = raw.business_name.strip() if raw.business_name else None
            cleaned_phone = normalize_phone(raw.phone)
            cleaned_website = raw.website.strip() if raw.website else None
            domain = extract_domain(cleaned_website)

            if not cleaned_name:
                # Bad row — stamp so we don't keep re-scanning it.
                raw.processed_at = now
                continue

            # -- Find existing business via in-memory lookup --
            existing_business = None
            if domain and domain in existing_by_domain:
                existing_business = existing_by_domain[domain]
            elif cleaned_phone and (cleaned_name, cleaned_phone) in existing_by_name_phone:
                existing_business = existing_by_name_phone[(cleaned_name, cleaned_phone)]

            if existing_business:
                business_id = existing_business.id
                if not existing_business.phone and cleaned_phone:
                    existing_business.phone = cleaned_phone
                    existing_by_name_phone[(cleaned_name, cleaned_phone)] = existing_business
                if not existing_business.website and cleaned_website:
                    existing_business.website = cleaned_website
                    existing_business.domain = domain
                    if domain:
                        existing_by_domain[domain] = existing_business
            else:
                new_business = Business(
                    business_name=cleaned_name,
                    category=raw.category,
                    website=cleaned_website,
                    domain=domain,
                    phone=cleaned_phone,
                )
                session.add(new_business)
                session.flush()  # populate .id
                business_id = new_business.id
                businesses_added += 1
                # Register the new business in our lookup dicts so subsequent
                # raw rows in this same run also dedupe against it.
                if domain:
                    existing_by_domain[domain] = new_business
                if cleaned_phone:
                    existing_by_name_phone[(cleaned_name, cleaned_phone)] = new_business

            # -- Insert Contacts (emails first; phone-only fallback if none) --
            emails = _parse_and_validate_emails(raw.email)

            if emails:
                for email in emails:
                    key = (business_id, email)
                    if key in existing_emails:
                        continue
                    session.add(Contact(
                        business_id=business_id,
                        name="Info/Office",
                        phone=cleaned_phone,
                        title="General Contact",
                        email=email,
                        lead_status="Not Contacted",
                    ))
                    existing_emails.add(key)
                    contacts_added += 1
            elif cleaned_phone:
                key = (business_id, cleaned_phone)
                if key not in existing_phones:
                    session.add(Contact(
                        business_id=business_id,
                        name="Info/Office",
                        phone=cleaned_phone,
                        title="General Contact",
                        email=None,
                        lead_status="Not Contacted",
                    ))
                    existing_phones.add(key)
                    contacts_added += 1

            raw.processed_at = now

        session.commit()
        print("Leads processing completed.")
        print(f"Added {businesses_added} new businesses to 'businesses' table.")
        print(f"Added {contacts_added} new contacts to 'contacts' table.")

    except Exception as e:
        session.rollback()
        print(f"Error during lead processing: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    process_and_deduplicate_leads()
