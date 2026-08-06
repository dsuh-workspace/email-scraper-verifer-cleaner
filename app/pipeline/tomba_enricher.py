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
        return []

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


def enrich_businesses_with_tomba(fallback_only: bool = True) -> int:
    """
    Enrich businesses in DB using Tomba Domain Search.

    :param fallback_only: If True, only query Tomba for businesses that have a domain
                          BUT NO email in Contact yet (saves API credits).
                          If False, query Tomba for all businesses with domains.
    :return: Number of new contacts added via Tomba.
    """
    if not TOMBA_API_KEY or not TOMBA_SECRET_KEY:
        logger.warning(
            "Skipping Tomba enrichment: TOMBA_API_KEY or TOMBA_SECRET_KEY is not set in .env."
        )
        return 0

    from sqlalchemy import func
    from sqlalchemy.orm import sessionmaker

    from app.db.create_tables import Business, Contact, ExportHistory, ScrapeRun
    from app.db.database import engine

    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        run_id = session.query(func.max(ScrapeRun.id)).scalar()

        if fallback_only:
            # Query businesses with domain BUT no email in Contact
            already_contacted = {
                row[0]
                for row in session.query(Contact.business_id)
                .filter(Contact.email.isnot(None))
                .distinct()
            }
            target_businesses = [
                b for b in session.query(Business).filter(Business.domain.isnot(None)).all()
                if b.id not in already_contacted
            ]
        else:
            target_businesses = (
                session.query(Business)
                .filter(Business.domain.isnot(None))
                .all()
            )

        logger.info(
            "Running Tomba enrichment for %d businesses (fallback_only=%s)...",
            len(target_businesses),
            fallback_only,
        )

        added_total = 0
        for i, biz in enumerate(target_businesses, start=1):
            if not biz.domain:
                continue

            records = fetch_domain_emails_from_tomba(biz.domain)
            if not records:
                continue

            existing_contacts = (
                session.query(Contact)
                .filter(Contact.business_id == biz.id)
                .all()
            )
            existing_emails = {c.email for c in existing_contacts if c.email}
            placeholders = [c for c in existing_contacts if c.email is None]

            added_for_biz = 0
            for rec in records:
                email = rec["email"]
                if email in existing_emails:
                    continue

                new_contact = Contact(
                    business_id=biz.id,
                    name=rec["name"],
                    phone=rec["phone"] or biz.phone,
                    title=rec["title"],
                    email=email,
                    lead_status="Not Contacted",
                    first_scrape_run_id=run_id,
                )
                session.add(new_contact)
                existing_emails.add(email)
                added_for_biz += 1

            if added_for_biz > 0:
                for ph in placeholders:
                    session.query(ExportHistory).filter(
                        ExportHistory.contact_id == ph.id
                    ).delete(synchronize_session=False)
                    session.delete(ph)
                logger.info(
                    "[%d/%d] Tomba found %d emails for %s",
                    i, len(target_businesses), added_for_biz, biz.domain,
                )

            added_total += added_for_biz

            if i % 25 == 0:
                session.commit()

        session.commit()
        logger.info(
            "Tomba enrichment complete. Added %d new decision-maker contacts.",
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
