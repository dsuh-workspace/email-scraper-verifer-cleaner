"""
Saleshandy 12-Permutation Campaign Exporter & Automated API Pusher.

Sorts contacts from database/hvac_leads.db into 12 distinct Saleshandy campaign cohorts based on:
  1. Trade: HVAC vs Plumbing
  2. Persona: Owner vs Non-Owner
  3. Phone Classification: IVR vs Receptionist vs Voicemail

Can export 12 pre-sorted CSV files and/or push directly into Saleshandy Sequence APIs.
"""

from __future__ import annotations
import csv
import logging
import os
import re
import requests
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy.orm import sessionmaker

from app.db.database import engine

logger = logging.getLogger(__name__)
load_dotenv()

Session = sessionmaker(bind=engine)

# Trade keyword rules
HVAC_RE = re.compile(r"hvac|heating|cooling|furnace|air condition|heat pump|boiler|ductwork|\ba/?c\b", re.IGNORECASE)
PLUMBING_RE = re.compile(r"plumb|drain|sewer|septic|water heater|rooter|repipe", re.IGNORECASE)

# Owner title keywords
OWNER_TITLE_RE = re.compile(r"owner|founder|president|ceo|partner|principal|director|operator|manager", re.IGNORECASE)
GENERIC_NAME_RE = re.compile(r"info|office|admin|team|contact|support|service|decision maker", re.IGNORECASE)

# Saleshandy API credentials & sequence mapping
SALESHANDY_API_KEY = os.getenv("SALESHANDY_API_KEY")
SALESHANDY_API_URL = os.getenv("SALESHANDY_API_URL", "https://open-api.saleshandy.com/v1/sequences")

SEQUENCE_ID_MAP = {
    "HVAC_Owner_IVR": os.getenv("SH_SEQ_HVAC_OWNER_IVR"),
    "HVAC_Owner_Receptionist": os.getenv("SH_SEQ_HVAC_OWNER_RECEPTIONIST"),
    "HVAC_Owner_Voicemail": os.getenv("SH_SEQ_HVAC_OWNER_VOICEMAIL"),
    "HVAC_NonOwner_IVR": os.getenv("SH_SEQ_HVAC_NONOWNER_IVR"),
    "HVAC_NonOwner_Receptionist": os.getenv("SH_SEQ_HVAC_NONOWNER_RECEPTIONIST"),
    "HVAC_NonOwner_Voicemail": os.getenv("SH_SEQ_HVAC_NONOWNER_VOICEMAIL"),
    "Plumbing_Owner_IVR": os.getenv("SH_SEQ_PLUMBING_OWNER_IVR"),
    "Plumbing_Owner_Receptionist": os.getenv("SH_SEQ_PLUMBING_OWNER_RECEPTIONIST"),
    "Plumbing_Owner_Voicemail": os.getenv("SH_SEQ_PLUMBING_OWNER_VOICEMAIL"),
    "Plumbing_NonOwner_IVR": os.getenv("SH_SEQ_PLUMBING_NONOWNER_IVR"),
    "Plumbing_NonOwner_Receptionist": os.getenv("SH_SEQ_PLUMBING_NONOWNER_RECEPTIONIST"),
    "Plumbing_NonOwner_Voicemail": os.getenv("SH_SEQ_PLUMBING_NONOWNER_VOICEMAIL"),
}


def classify_trade(business) -> str:
    """Classify business as HVAC or Plumbing."""
    cat = (business.category or "").strip()
    name = (business.business_name or "").strip()
    desc = (business.description or "").strip()

    # Primary check on category first
    if cat:
        if HVAC_RE.search(cat) and not PLUMBING_RE.search(cat):
            return "HVAC"
        if PLUMBING_RE.search(cat) and not HVAC_RE.search(cat):
            return "Plumbing"

    # Secondary check on combined text
    text = f"{cat} {name} {desc}"
    if HVAC_RE.search(text) and not PLUMBING_RE.search(text):
        return "HVAC"
    if PLUMBING_RE.search(text):
        return "Plumbing"
    if HVAC_RE.search(text):
        return "HVAC"

    return "Plumbing"


# Role email prefixes that must be suppressed to protect domain sender reputation
SUPPRESSED_EMAIL_PREFIXES = (
    "careers@", "jobs@", "billing@", "accounting@", "hr@", "press@",
    "media@", "legal@", "webmaster@", "abuse@", "postmaster@",
    "hostmaster@", "compliance@", "finance@", "invoices@", "payables@",
    "gdpr@", "copyright@", "unsubscribe@", "no-reply@", "noreply@"
)

LEGAL_SUFFIXES_RE = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|corp|corp\.|corporation|co|co\.|company|ltd|ltd\.|limited|p\.c\.|pc)\b",
    re.IGNORECASE,
)
SEPARATORS_RE = re.compile(r"\s*[\|-].*$", re.IGNORECASE)


def clean_company_name(name: str | None) -> str:
    """Clean company name by removing legal suffixes and keyword stuffing."""
    if not name:
        return "your business"

    cleaned = name.strip()
    # Strip pipe or hyphen separator keyword stuffing (e.g. "Apex Plumbing - 24/7 Service" -> "Apex Plumbing")
    cleaned = SEPARATORS_RE.sub("", cleaned).strip()
    
    # Strip trailing legal suffixes (Inc, LLC, Corp, etc.)
    cand = LEGAL_SUFFIXES_RE.sub("", cleaned).strip(" ,.-")
    
    # Keep candidate if it leaves at least 2 words, or if original had more than 2 words
    if cand and len(cand.split()) >= 2:
        return cand
    elif cand and len(cleaned.split()) > 2:
        return cand

    return cleaned if cleaned else (name.strip() or "your business")


def classify_persona(contact) -> str | None:
    """Classify contact as Owner vs NonOwner. Returns None if email prefix is suppressed."""
    name = (contact.name or "").strip()
    title = (contact.title or "").strip()
    email = (contact.email or "").strip().lower()

    # Suppress complaint-prone role-based email prefixes
    if any(email.startswith(prefix) for prefix in SUPPRESSED_EMAIL_PREFIXES):
        return None

    # Generic email prefix check for NonOwner team/info inboxes
    if any(email.startswith(prefix) for prefix in ("info@", "office@", "contact@", "admin@", "support@", "service@", "privacy@", "sales@", "team@", "help@")):
        return "NonOwner"

    # Generic name check
    if GENERIC_NAME_RE.search(name):
        return "NonOwner"

    # Owner title check
    if OWNER_TITLE_RE.search(title) or title == "Executive / Decision Maker":
        return "Owner"

    # Real first + last name present
    if len(name.split()) >= 2 and name not in ("Info/Office", "Decision Maker"):
        return "Owner"

    return "NonOwner"


def classify_phone_type(contact) -> str:
    """Classify phone destination as IVR, Receptionist, Voicemail, or Disconnected."""
    status = (contact.lead_status or "").strip()

    if any(k in status for k in ("Disconnected", "Invalid", "Failed", "Dead")):
        return "Disconnected"
    elif "IVR" in status:
        return "IVR"
    elif "Receptionist" in status or "Human" in status:
        return "Receptionist"
    elif "Voicemail" in status:
        return "Voicemail"

    # Fallback default classification for uncalled contacts
    return "Voicemail"


def sort_database_into_12_buckets(session, min_score: int = 0, exclude_unexported: bool = False) -> dict[str, list[dict]]:
    """Query contacts and sort into 12 permutation buckets.
    
    Enforces Persona Priority: If a business has an Owner contact, only the Owner
    contact is enrolled; NonOwner contacts for the same business are skipped.
    
    :param session: SQLAlchemy session.
    :param min_score: Minimum verification score filter (0 means no gating).
    :param exclude_unexported: If True, exclude contacts previously exported to Saleshandy via ExportHistory.
    """
    from collections import defaultdict
    from datetime import datetime, timezone
    from app.db.create_tables import Contact, Business, ExportHistory
    from app.pipeline.call_leads import reconcile_stale_phone_classifications

    # Step 1: Reconcile any stale pending phone calls before sorting
    reconcile_stale_phone_classifications(session=session, timeout_hours=2.0)

    # Step 2: Build Base Query
    query = (
        session.query(Contact, Business)
        .join(Business, Contact.business_id == Business.id)
        .filter(Contact.email.isnot(None))
        .filter(Contact.email != "")
    )

    if min_score > 0:
        from app.db.create_tables import EmailVerification
        query = (
            query.join(EmailVerification, EmailVerification.contact_id == Contact.id)
            .filter(EmailVerification.score >= min_score)
        )

    if exclude_unexported:
        from sqlalchemy import select
        exported_ids_stmt = (
            select(ExportHistory.contact_id)
            .where(ExportHistory.destination.like("saleshandy%"))
        )
        query = query.filter(~Contact.id.in_(exported_ids_stmt))

    contacts_query = query.all()

    # Step 3: Group contacts by business and apply Persona Priority Rule (Owner > NonOwner)
    biz_map = defaultdict(list)
    for contact, business in contacts_query:
        persona = classify_persona(contact)
        if persona is None:  # Suppressed email prefix
            continue
        biz_map[business.id].append((contact, business, persona))

    prioritized_contacts = []
    for biz_id, contact_triples in biz_map.items():
        owners = [t for t in contact_triples if t[2] == "Owner"]
        if owners:
            # Business has Owner(s) -> enroll only Owner contact(s), drop NonOwners
            prioritized_contacts.extend(owners)
        else:
            # Business has no Owner -> enroll NonOwner contact(s)
            prioritized_contacts.extend(contact_triples)

    logger.info("Sorting %d prioritized contacts into 12 Saleshandy permutations (min_score=%d)...", len(prioritized_contacts), min_score)

    buckets: dict[str, list[dict]] = {
        f"{trade}_{persona}_{phone}": []
        for trade in ("HVAC", "Plumbing")
        for persona in ("Owner", "NonOwner")
        for phone in ("IVR", "Receptionist", "Voicemail")
    }

    for contact, business, persona in prioritized_contacts:
        trade = classify_trade(business)
        phone_type = classify_phone_type(contact)

        # Disconnected lines won't match the 12 active sequence buckets and get excluded automatically
        permutation_tag = f"{trade}_{persona}_{phone_type}"

        name_parts = (contact.name or "").strip().split(maxsplit=1)
        first_name = name_parts[0] if name_parts else ("there" if persona == "Owner" else "Team")
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        demo_phone = "472-244-1040" if trade == "HVAC" else "472-244-1040"
        cleaned_company = clean_company_name(business.business_name)

        record = {
            "Contact ID": contact.id,
            "First Name": first_name,
            "Last Name": last_name,
            "Email": contact.email,
            "Phone": contact.phone or business.phone or "",
            "Company": cleaned_company,
            "Job Title": contact.title or "",
            "Trade": trade,
            "Persona": persona,
            "Phone Classification": phone_type,
            "Permutation Tag": permutation_tag,
            "Demo Phone": demo_phone,
            "Website": business.website or "",
            "Address": business.address or "",
            "Lead Status": contact.lead_status or "",
        }

        if permutation_tag in buckets:
            buckets[permutation_tag].append(record)

    return buckets


def export_12_saleshandy_permutations(output_dir: str = "data/saleshandy_campaigns", min_score: int = 0, exclude_unexported: bool = False) -> dict[str, int]:
    """Export 12 pre-sorted CSV files for manual/batch Saleshandy import."""
    from datetime import datetime, timezone
    from app.db.create_tables import ExportHistory

    session = Session()
    try:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        buckets = sort_database_into_12_buckets(session, min_score=min_score, exclude_unexported=exclude_unexported)
        summary_counts: dict[str, int] = {}

        fieldnames = [
            "Contact ID", "First Name", "Last Name", "Email", "Phone", "Company",
            "Job Title", "Trade", "Persona", "Phone Classification",
            "Permutation Tag", "Demo Phone", "Website", "Address", "Lead Status"
        ]

        for perm_tag, records in buckets.items():
            csv_file = out_path / f"saleshandy_{perm_tag.lower()}.csv"
            summary_counts[perm_tag] = len(records)

            with open(csv_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(records)

            # Stamp export history for exported contacts
            for rec in records:
                cid = rec.get("Contact ID")
                if cid:
                    session.add(ExportHistory(
                        contact_id=cid,
                        destination=f"saleshandy_{perm_tag.lower()}",
                        exported_at=datetime.now(timezone.utc)
                    ))

            logger.info("Exported %d leads to Saleshandy campaign file: %s", len(records), csv_file)

        session.commit()
        return summary_counts
    except Exception as e:
        session.rollback()
        logger.error("Error exporting Saleshandy permutations: %s", e)
        raise
    finally:
        session.close()


def push_to_saleshandy_api(min_score: int = 0, exclude_unexported: bool = True) -> dict[str, int]:
    """
    Automated API Push: Uploads prospects directly to Saleshandy sequence endpoints.
    Uses persistent HTTP session, connection pooling, and exponential backoff retry logic.
    Requires SALESHANDY_API_KEY and SH_SEQ_* sequence IDs in .env.
    """
    if not SALESHANDY_API_KEY:
        logger.warning("Skipping Saleshandy API push: SALESHANDY_API_KEY is not set in .env")
        return {}

    from datetime import datetime, timezone
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from app.db.create_tables import ExportHistory

    session_db = Session()
    try:
        buckets = sort_database_into_12_buckets(session_db, min_score=min_score, exclude_unexported=exclude_unexported)
        results: dict[str, int] = {}

        # Setup HTTP Session with retry adapter for resilience
        http_session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        http_session.mount("https://", adapter)
        http_session.mount("http://", adapter)

        headers = {
            "x-api-key": SALESHANDY_API_KEY,
            "Content-Type": "application/json",
        }

        for perm_tag, records in buckets.items():
            seq_id = SEQUENCE_ID_MAP.get(perm_tag)
            if not seq_id:
                logger.warning("No sequence ID mapped in .env for permutation %s (set SH_SEQ_%s)", perm_tag, perm_tag.upper())
                results[perm_tag] = 0
                continue

            push_url = f"{SALESHANDY_API_URL.rstrip('/')}/{seq_id}/prospects"
            pushed_count = 0

            for rec in records:
                payload = {
                    "email": rec["Email"],
                    "first_name": rec["First Name"],
                    "last_name": rec["Last Name"],
                    "company_name": rec["Company"],
                    "phone_number": rec["Phone"],
                    "job_title": rec["Job Title"],
                    "custom_fields": {
                        "demo_phone": rec["Demo Phone"],
                        "trade": rec["Trade"],
                        "phone_classification": rec["Phone Classification"],
                    }
                }

                try:
                    resp = http_session.post(push_url, json=payload, headers=headers, timeout=15)
                    if resp.status_code in (200, 201):
                        pushed_count += 1
                        cid = rec.get("Contact ID")
                        if cid:
                            session_db.add(ExportHistory(
                                contact_id=cid,
                                destination="saleshandy",
                                exported_at=datetime.now(timezone.utc)
                            ))
                    else:
                        logger.warning("Saleshandy API push failed for %s to seq %s: HTTP %d — %s", rec["Email"], seq_id, resp.status_code, resp.text[:200])
                except Exception as e:
                    logger.error("Error pushing %s to Saleshandy: %s", rec["Email"], e)

            session_db.commit()
            logger.info("Successfully pushed %d prospects to Saleshandy sequence %s (%s)", pushed_count, seq_id, perm_tag)
            results[perm_tag] = pushed_count

        return results
    except Exception as e:
        session_db.rollback()
        logger.error("Error pushing leads to Saleshandy API: %s", e)
        raise
    finally:
        session_db.close()


if __name__ == "__main__":
    from app.logging_config import setup_logging
    setup_logging()
    export_12_saleshandy_permutations()
