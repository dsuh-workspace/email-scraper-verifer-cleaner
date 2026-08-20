"""
Tomba Email Finder & Decision Maker Enricher.

Uses Tomba's Domain Search API (https://api.tomba.io/v1/domain-search) to find
decision-maker emails, names, and job titles for businesses in the DB.

Configuration (in .env):
    TOMBA_API_KEY=ta_...
    TOMBA_SECRET_KEY=ts_...
"""

import logging
import os
import time
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

TOMBA_API_KEY = os.getenv("TOMBA_API_KEY") or os.getenv("TOMBA_KEY")
TOMBA_SECRET_KEY = os.getenv("TOMBA_SECRET_KEY") or os.getenv("TOMBA_SECRET")
TOMBA_DOMAIN_SEARCH_URL = "https://api.tomba.io/v1/domain-search"
TOMBA_TIMEOUT_SEC = 15


def fetch_domain_emails_from_tomba(domain: str, limit: int = 10) -> List[Dict]:
    """
    Call Tomba Domain Search API for a single domain.

    Returns a list of dicts:
    [
        {
            "email": "john.doe@company.com",
            "name": "John Doe",
            "title": "Owner",
            "phone": "+1...",
            "score": 90
        },
        ...
    ]
    """
    if not TOMBA_API_KEY or not TOMBA_SECRET_KEY:
        logger.error(
            "Tomba API key or Secret key not configured in environment "
            "(set TOMBA_API_KEY and TOMBA_SECRET_KEY in .env)."
        )
        return []

    headers = {
        "X-Tomba-Key": TOMBA_API_KEY,
        "X-Tomba-Secret": TOMBA_SECRET_KEY,
        "Accept": "application/json",
    }
    params = {
        "domain": domain,
        "limit": limit,
    }

    response = None
    for attempt in range(3):
        try:
            response = requests.get(
                TOMBA_DOMAIN_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=TOMBA_TIMEOUT_SEC,
            )
            if response.status_code == 429:
                logger.warning("Tomba API rate limit hit (429). Cooldown for 3 seconds (attempt %d/3)...", attempt + 1)
                time.sleep(3.0)
                continue
            break
        except requests.RequestException as e:
            logger.warning("Tomba API request failed for domain %s: %s", domain, e)
            return []

    if response is None or response.status_code in (401, 403):
        logger.error(
            "Tomba API authentication failed (HTTP %s). "
            "Please check your TOMBA_API_KEY and TOMBA_SECRET_KEY.",
            getattr(response, 'status_code', 'N/A'),
        )
        return []
    elif response.status_code != 200:
        logger.warning(
            "Tomba API returned HTTP %d for %s: %s",
            response.status_code, domain, response.text[:200],
        )
        time.sleep(1.0)
        return []

    time.sleep(1.5)  # Steady pacing to respect Tomba rate limits (40 RPM)

    try:
        data = response.json()
    except ValueError as e:
        logger.warning("Tomba returned non-JSON for %s: %s", domain, e)
        return []

    results = []
    data_obj = data.get("data", {})
    emails_list = data_obj.get("emails", [])

    for item in emails_list:
        email = item.get("email")
        if not email or "@" not in email:
            continue

        first_name = (item.get("first_name") or "").strip()
        last_name = (item.get("last_name") or "").strip()
        name_parts = [p for p in (first_name, last_name) if p]
        name = " ".join(name_parts) if name_parts else "Decision Maker"

        position = (item.get("position") or "").strip() or "Executive / Decision Maker"
        phone_val = item.get("phone_number")
        phone = phone_val if isinstance(phone_val, str) and phone_val.strip() else None
        score = item.get("score")

        results.append({
            "email": email,
            "name": name,
            "title": position,
            "phone": phone,
            "score": score,
        })

    return results


def enrich_businesses_with_tomba(
    fallback_only: bool = True,
    scrape_run_id: Optional[int] = None,
    exclude_already_exported: bool = True,
) -> int:
    """
    Enrich businesses in DB using Tomba Domain Search with 2-Gate Early Deduplication.

    Gate 1 (Pre-Tomba):
      - Skips businesses whose domain or ID is already in ExportHistory (saves API credits).
      - Skips businesses that already have an Owner contact with an email.

    Gate 2 (Post-Tomba Ingest):
      - Checks returned emails against global ExportHistory (never re-ingest previously exported emails).
      - Deduplicates against global DB contacts and applies Persona Priority (Owner > Non-Owner).

    :param fallback_only: If True, only query Tomba for businesses that have a domain
                          BUT NO email in Contact yet.
    :param scrape_run_id: Optional scrape run ID to restrict enrichment strictly to the current run cohort.
    :param exclude_already_exported: If True (default), skip businesses already exported to Saleshandy.
    :return: Number of new contacts added via Tomba.
    """
    if not TOMBA_API_KEY or not TOMBA_SECRET_KEY:
        logger.warning(
            "Skipping Tomba enrichment: TOMBA_API_KEY or TOMBA_SECRET_KEY is not set in .env."
        )
        return 0

    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker

    from app.db.create_tables import Business, Contact, ExportHistory, ScrapeRun
    from app.db.database import engine

    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        run_id = scrape_run_id or session.query(func.max(ScrapeRun.id)).scalar()

        # Gate 1: Build base target businesses query
        biz_query = session.query(Business).filter(Business.domain.isnot(None))
        if scrape_run_id is not None:
            biz_query = biz_query.filter(Business.first_scrape_run_id == scrape_run_id)

        # Exclude businesses already exported to Saleshandy if requested
        if exclude_already_exported:
            exported_biz_ids = {
                row[0]
                for row in session.query(Contact.business_id)
                .join(ExportHistory, ExportHistory.contact_id == Contact.id)
                .where(ExportHistory.destination.like("saleshandy_api%"))
                .distinct()
            }
        else:
            exported_biz_ids = set()

        if fallback_only:
            # Query businesses with domain BUT no email in Contact
            already_contacted = {
                row[0]
                for row in session.query(Contact.business_id)
                .filter(Contact.email.isnot(None))
                .filter(Contact.email != "")
                .distinct()
            }
            target_businesses = [
                b for b in biz_query.all()
                if b.id not in already_contacted and b.id not in exported_biz_ids
            ]
        else:
            target_businesses = [
                b for b in biz_query.all()
                if b.id not in exported_biz_ids
            ]

        logger.info(
            "Running Tomba enrichment for %d businesses (run_id=%s, fallback_only=%s, excluded_exported_biz=%d)...",
            len(target_businesses),
            run_id,
            fallback_only,
            len(exported_biz_ids),
        )

        if not target_businesses:
            return 0

        # Gate 2 Setup: Global email blacklists across entire database & export history
        global_exported_emails = {
            row[0].strip().lower()
            for row in session.query(func.lower(Contact.email))
            .join(ExportHistory, ExportHistory.contact_id == Contact.id)
            .filter(ExportHistory.destination.like("saleshandy_api%"))
            .filter(Contact.email.isnot(None))
            .filter(Contact.email != "")
            .distinct()
            if row[0]
        }

        global_existing_emails = {
            row[0].strip().lower()
            for row in session.query(func.lower(Contact.email))
            .filter(Contact.email.isnot(None))
            .filter(Contact.email != "")
            .distinct()
            if row[0]
        }

        added_total = 0
        for i, biz in enumerate(target_businesses, start=1):
            if not biz.domain:
                continue

            records = fetch_domain_emails_from_tomba(biz.domain)
            if not records:
                continue

            # Prioritize records: Owner/President/Founder first
            owner_keywords = ("owner", "president", "founder", "principal", "ceo", "partner")
            records.sort(
                key=lambda r: (
                    any(kw in (r.get("title") or "").lower() for kw in owner_keywords),
                    r.get("score") or 0,
                ),
                reverse=True,
            )

            existing_contacts = (
                session.query(Contact)
                .filter(Contact.business_id == biz.id)
                .all()
            )
            existing_biz_emails = {c.email.strip().lower() for c in existing_contacts if c.email}
            placeholders = [c for c in existing_contacts if not c.email]

            added_for_biz = 0
            for rec in records:
                raw_email = (rec.get("email") or "").strip()
                email_lower = raw_email.lower()
                if not raw_email or "@" not in raw_email:
                    continue

                # Gate 2 Checks:
                # 1. Has this email already been exported to Saleshandy previously?
                if email_lower in global_exported_emails:
                    logger.debug("Gate 2 Dedupe: Skipping Tomba email %s (already exported to Saleshandy)", raw_email)
                    continue

                # 2. Is this email already in this business or anywhere in the DB?
                if email_lower in existing_biz_emails or email_lower in global_existing_emails:
                    continue

                new_contact = Contact(
                    business_id=biz.id,
                    name=rec["name"],
                    phone=rec["phone"] or biz.phone,
                    title=rec["title"],
                    email=raw_email,
                    lead_status="Not Contacted",
                    first_scrape_run_id=run_id,
                )
                session.add(new_contact)
                existing_biz_emails.add(email_lower)
                global_existing_emails.add(email_lower)
                added_for_biz += 1

                # If we found an explicit Owner/Executive, don't spam 5 extra generic inboxes for the same contractor
                if any(kw in (rec.get("title") or "").lower() for kw in owner_keywords):
                    break

            if added_for_biz > 0:
                for ph in placeholders:
                    session.query(ExportHistory).filter(
                        ExportHistory.contact_id == ph.id
                    ).delete(synchronize_session=False)
                    session.delete(ph)
                logger.info(
                    "[%d/%d] Tomba found %d new decision-maker emails for %s",
                    i, len(target_businesses), added_for_biz, biz.domain,
                )

            added_total += added_for_biz

            if i % 25 == 0:
                session.commit()

        session.commit()
        logger.info(
            "Tomba enrichment complete. Added %d new decision-maker contacts (Gate 2 deduplicated).",
            added_total,
        )
        return added_total
    except Exception as e:
        session.rollback()
        logger.error("Error during Tomba enrichment: %s", e)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    from app.logging_config import setup_logging
    setup_logging()
    enrich_businesses_with_tomba(fallback_only=True)
