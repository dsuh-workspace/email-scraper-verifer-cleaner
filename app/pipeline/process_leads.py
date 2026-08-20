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
- Splits raw email strings on ,;\\s and validates candidates with
  `email-validator` so we don't insert garbage into Contact.email.
"""

import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Tuple

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from app.logging_config import setup_logging
from app.pipeline.email_filters import is_junk_email

logger = logging.getLogger(__name__)

# Raw scraper output separates emails with any of these — treat them all.
_EMAIL_SPLIT_RE = re.compile(r'[,;\s]+')

_HVAC_RE = re.compile(r"hvac|heating|cooling|furnace|air condition|heat pump|boiler|ductwork|\ba/?c\b", re.IGNORECASE)
_PLUMBING_RE = re.compile(r"plumb|drain|sewer|septic|water heater|rooter|repipe", re.IGNORECASE)


def _determine_primary_trade(name: str | None, cat: str | None, desc: str | None) -> str:
    """Determine HVAC vs Plumbing trade based on name and category signals."""
    name_str = (name or "").strip()
    cat_str = (cat or "").strip()
    desc_str = (desc or "").strip()

    name_hvac = bool(_HVAC_RE.search(name_str))
    name_plumb = bool(_PLUMBING_RE.search(name_str))

    # 1. High-confidence explicit business name check
    if name_plumb and not name_hvac:
        return "Plumbing"
    if name_hvac and not name_plumb:
        return "HVAC"

    # 2. Category check
    cat_hvac = bool(_HVAC_RE.search(cat_str))
    cat_plumb = bool(_PLUMBING_RE.search(cat_str))
    if cat_plumb and not cat_hvac:
        return "Plumbing"
    if cat_hvac and not cat_plumb:
        return "HVAC"

    # 3. Combined text fallback
    text = f"{name_str} {cat_str} {desc_str}"
    if _HVAC_RE.search(text) and not _PLUMBING_RE.search(text):
        return "HVAC"
    if _PLUMBING_RE.search(text) and not _HVAC_RE.search(text):
        return "Plumbing"

    return "HVAC" if _HVAC_RE.search(text) and not _PLUMBING_RE.search(text) else "Plumbing"


def _merge_categories(existing: str | None, incoming: str | None) -> str | None:
    """Merge and deduplicate comma-separated GMB category lists non-destructively."""
    if not existing:
        return incoming.strip() if incoming else None
    if not incoming:
        return existing.strip()

    parts_existing = [p.strip() for p in existing.split(",") if p.strip()]
    parts_incoming = [p.strip() for p in incoming.split(",") if p.strip()]

    seen_lower = {p.lower() for p in parts_existing}
    merged = list(parts_existing)
    for p in parts_incoming:
        if p.lower() not in seen_lower:
            merged.append(p)
            seen_lower.add(p.lower())
    return ", ".join(merged)


def extract_domain(url_str):
    """
    Extract the base domain (e.g. 'rotorooter.com') from a website URL.
    """
    if not url_str:
        return None
    try:
        raw = url_str.strip()
        if not raw:
            return None
        parsed = urllib.parse.urlparse(raw if '://' in raw else f'http://{raw}')
        domain = (parsed.hostname or "").lower()
        return domain.removeprefix('www.') or None
    except Exception:
        return None


def normalize_phone(phone_str):
    """
    Normalize to E.164-ish (+1XXXXXXXXXX). Falls back to the stripped
    original if the digit count doesn't look like a US number.
    """
    if not phone_str:
        return None

    raw = phone_str.strip()
    digits = re.sub(r'\D', '', raw)
    if not digits:
        return None

    if len(digits) == 10:
        prefix = "+1"
    elif len(digits) == 11 and digits.startswith('1'):
        prefix = "+"
    else:
        return raw

    try:
        parsed = phonenumbers.parse(digits, "US")
        if phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return f"{prefix}{digits}"


def _parse_and_validate_emails(raw_email_field: str):
    """
    Split a scraper email field (which can contain commas, semicolons,
    or whitespace between addresses) and drop anything that doesn't
    validate as an email.
    """
    if not raw_email_field:
        return []
    parts = _EMAIL_SPLIT_RE.split(raw_email_field.strip())
    valid = []
    seen = set()
    for part in parts:
        candidate = part.strip().lower().rstrip('.,;')
        if not candidate or candidate in seen:
            continue
        try:
            email = validate_email(candidate, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            continue
        # Shared with the crawler and the export gate (email_filters.py).
        # This side matters as much as the crawler's: a retina asset filename
        # like "about-300x281@2x.png" passes validate_email, and until these
        # two paths were unified the scraper's own email field also carried
        # agency/CDN addresses the crawler had always rejected.
        if is_junk_email(email):
            continue
        if email in seen:
            continue
        seen.add(email)
        valid.append(email)
    return valid


def process_and_deduplicate_leads() -> None:
    """
    Main entrypoint. Processes unprocessed raw leads and promotes to
    businesses + contacts, deduping in memory to avoid N+1 queries.
    """
    from sqlalchemy.orm import sessionmaker

    from app.db.database import engine
    from app.db.create_tables import RawLead, Business, Contact

    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        # -- Load unprocessed raw leads (skip anything with processed_at set) --
        raw_leads = (
            session.query(RawLead)
            .filter(RawLead.processed_at.is_(None))
            .all()
        )
        logger.info(f"Loaded {len(raw_leads)} unprocessed raw leads for processing.")
        # -- Pre-load existing businesses into fast lookup dicts (one SELECT total) --
        # Skip garbage domain rows ('http:', 'https:', '') left by the legacy
        # case-sensitive scheme-check bug. Seeding those into the dedup map
        # collapses every subsequent website-less biz onto whichever row
        # happened to hold that key.
        _BAD_DOMAINS = {"http:", "https:", ""}
        existing_by_domain: Dict[str, Business] = {}
        existing_by_name_phone: Dict[Tuple[str, str], Business] = {}
        for biz in session.query(Business).all():
            if biz.domain and biz.domain not in _BAD_DOMAINS:
                existing_by_domain[biz.domain] = biz
            if biz.business_name and biz.phone:
                existing_by_name_phone[(biz.business_name, biz.phone)] = biz

        # -- Pre-load contact fingerprints so we don't re-add duplicates --
        # keyed by (business_id, email) and (business_id, phone)
        existing_emails: set = {
            (row[0], row[1])
            for row in session.query(Contact.business_id, Contact.email)
            .filter(Contact.email.isnot(None))
        }
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
                # Non-destructive category enrichment
                if raw.category:
                    existing_business.category = _merge_categories(existing_business.category, raw.category)
                if not existing_business.primary_trade:
                    existing_business.primary_trade = _determine_primary_trade(
                        existing_business.business_name,
                        existing_business.category,
                        existing_business.description
                    )
            else:
                trade = _determine_primary_trade(cleaned_name, raw.category, raw.description)
                new_business = Business(
                    business_name=cleaned_name,
                    category=raw.category,
                    primary_trade=trade,
                    website=cleaned_website,
                    domain=domain,
                    phone=cleaned_phone,
                    review_count=raw.review_count,
                    review_rating=raw.review_rating,
                    address=raw.address,
                    status=raw.status,
                    description=raw.description,
                    place_id=raw.place_id,
                    first_scrape_run_id=raw.scrape_run_id,
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
                from app.pipeline.export_saleshandy import extract_name_and_persona
                for email in emails:
                    key = (business_id, email)
                    if key in existing_emails:
                        continue

                    fn, ln, persona = extract_name_and_persona(type("TempContact", (), {"name": "", "title": "", "email": email}))
                    if persona == "Owner" and fn and fn != "there":
                        contact_name = f"{fn} {ln}".strip()
                        contact_title = "Owner / Decision Maker"
                    else:
                        contact_name = "Info/Office"
                        contact_title = "General Contact"

                    session.add(Contact(
                        business_id=business_id,
                        name=contact_name,
                        phone=cleaned_phone,
                        title=contact_title,
                        email=email,
                        lead_status="Not Contacted",
                        first_scrape_run_id=raw.scrape_run_id,
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
                        first_scrape_run_id=raw.scrape_run_id,
                    ))
                    existing_phones.add(key)
                    contacts_added += 1

            raw.processed_at = now

        session.commit()
        logger.info("Leads processing completed.")
        logger.info(f"Added {businesses_added} new businesses to 'businesses' table.")
        logger.info(f"Added {contacts_added} new contacts to 'contacts' table.")
    except Exception as e:
        session.rollback()
        logger.error(f"Error during lead processing: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    setup_logging()
    process_and_deduplicate_leads()