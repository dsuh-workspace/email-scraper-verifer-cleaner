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
    """Classify business as HVAC or Plumbing with database provenance and name priority."""
    # 0. Check database explicit override or stored primary_trade
    if hasattr(business, "trade_override") and business.trade_override:
        return business.trade_override
    if hasattr(business, "primary_trade") and business.primary_trade:
        return business.primary_trade

    name = (business.business_name or "").strip()
    cat = (business.category or "").strip()
    desc = (business.description or "").strip()

    name_hvac = bool(HVAC_RE.search(name))
    name_plumb = bool(PLUMBING_RE.search(name))

    # 1. High-confidence explicit business name check (e.g. "Bueno Plumbing and Rooter")
    if name_plumb and not name_hvac:
        return "Plumbing"
    if name_hvac and not name_plumb:
        return "HVAC"

    # 2. Check GMB categories when name is neutral or dual-trade
    cat_hvac = bool(HVAC_RE.search(cat))
    cat_plumb = bool(PLUMBING_RE.search(cat))
    if cat_plumb and not cat_hvac:
        return "Plumbing"
    if cat_hvac and not cat_plumb:
        return "HVAC"

    # 3. Combined text fallback
    text = f"{name} {cat} {desc}"
    if HVAC_RE.search(text) and not PLUMBING_RE.search(text):
        return "HVAC"
    if PLUMBING_RE.search(text) and not HVAC_RE.search(text):
        return "Plumbing"

    # 4. Final tiebreaker: default to Plumbing
    return "HVAC" if HVAC_RE.search(text) and not PLUMBING_RE.search(text) else "Plumbing"


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
EMOJI_SYMBOLS_RE = re.compile(r"[\U00010000-\U0010ffff\u2600-\u27ff\u2b50\u2714\u2716\u2728★✓•–—]+", re.UNICODE)
KNOWN_ACRONYMS = {"HVAC", "AC", "USA", "AAA", "CPI", "SWAGS", "24/7", "EJ", "JDD", "RCV", "UA", "OTP"}


def clean_company_name(name: str | None) -> str:
    """Clean company name by removing legal suffixes, promo/geo tags, emojis, and normalizing casing."""
    if not name:
        return "your business"

    cleaned = name.strip()

    # 1. Remove emojis and decorative symbols
    cleaned = EMOJI_SYMBOLS_RE.sub("", cleaned).strip()

    # 2. Strip pipe or hyphen separator keyword stuffing (e.g. "Apex Plumbing - 24/7 Service" -> "Apex Plumbing")
    cleaned = SEPARATORS_RE.sub("", cleaned).strip()

    # 3. Strip trailing legal suffixes (Inc, LLC, Corp, etc.)
    cand = LEGAL_SUFFIXES_RE.sub("", cleaned).strip(" ,.-")
    if cand and len(cand.split()) >= 2:
        cleaned = cand
    elif cand and len(cleaned.split()) > 2:
        cleaned = cand

    # 4. Handle ALL-CAPS names (e.g. "ECO HVAC CONTRACTING" -> "Eco HVAC Contracting")
    words = cleaned.split()
    if words and (cleaned.isupper() or sum(1 for w in words if w.isupper() and len(w) > 2) >= len(words) // 2 + 1):
        normalized_words = []
        for w in words:
            upper_w = w.upper().rstrip(".,")
            if upper_w in KNOWN_ACRONYMS:
                normalized_words.append(upper_w)
            elif w.lower() in {"and", "&", "of", "the", "for", "in"}:
                normalized_words.append(w.lower())
            else:
                normalized_words.append(w.capitalize())
        cleaned = " ".join(normalized_words)

    # 5. Clean extra whitespace
    cleaned = " ".join(cleaned.split()).strip(" ,.-")

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


def classify_phone_type(contact, business=None) -> str:
    """Classify phone destination as IVR, Receptionist, Voicemail, or Disconnected.
    
    1. If contact has completed a live phone classification call, respect audio transcript.
    2. If contact is Disconnected, Invalid, Failed, or Dead, return Disconnected (suppressed).
    3. If uncalled, use firmographic heuristics (review count, multi-service, generic email)
       and deterministic distribution to match empirical field ratios (~42% Receptionist, ~28% IVR, ~30% Voicemail).
    """
    import hashlib
    status = (getattr(contact, "lead_status", "") or "").strip()

    # 1. Explicit Disconnected or suppressed statuses
    if any(k in status for k in ("Disconnected", "Invalid", "Failed", "Dead")):
        return "Disconnected"

    # 2. Confirmed live phone audio classifications
    if "IVR" in status:
        return "IVR"
    elif "Receptionist" in status or "Human" in status:
        return "Receptionist"
    elif "Voicemail" in status and "Classified" in status:
        return "Voicemail"

    # 3. Firmographic heuristics for uncalled leads
    reviews = getattr(business, "review_count", 0) or 0
    categories = (getattr(business, "category", "") or "").lower()
    email = (getattr(contact, "email", "") or "").lower()
    title = (getattr(contact, "title", "") or "").lower()

    # High-review shops (60+ reviews) or multi-category businesses almost always have an IVR or Receptionist
    if reviews >= 80 or ("," in categories and reviews >= 40):
        h = int(hashlib.md5(f"ivr_rec_{getattr(contact, 'id', 0)}_{email}".encode()).hexdigest(), 16) % 100
        return "IVR" if h < 60 else "Receptionist"

    # Generic office / dispatch emails strongly correlate with a receptionist / front desk
    if any(email.startswith(p) for p in ("info@", "office@", "service@", "contact@", "dispatch@", "support@", "admin@")) or \
       any(w in title for w in ("receptionist", "front desk", "office", "coordinator", "dispatcher")):
        return "Receptionist"

    # Small single-owner shops (<=15 reviews, Owner persona) strongly correlate with direct voicemail
    if 0 < reviews <= 15 and any(w in title for w in ("owner", "founder", "president", "principal")):
        h = int(hashlib.md5(f"vm_owner_{getattr(contact, 'id', 0)}_{email}".encode()).hexdigest(), 16) % 100
        return "Voicemail" if h < 75 else "Receptionist"

    # Balanced deterministic distribution for general uncalled leads matching empirical ratios:
    # 42% Receptionist, 28% IVR, 30% Voicemail
    key = f"{getattr(business, 'business_name', '')}_{email}_{getattr(contact, 'id', 0)}"
    h = int(hashlib.md5(key.encode()).hexdigest(), 16) % 100
    if h < 42:
        return "Receptionist"
    elif h < 70:
        return "IVR"
    else:
        return "Voicemail"


def sort_database_into_12_buckets(session, min_score: int = 0, exclude_unexported: bool = False, destination_prefix: str = "saleshandy", only_classified: bool = False) -> dict[str, list[dict]]:
    """Query contacts and sort into 12 permutation buckets.
    
    Enforces Persona Priority: If a business has an Owner contact, only the Owner
    contact is enrolled; NonOwner contacts for the same business are skipped.
    
    :param session: SQLAlchemy session.
    :param min_score: Minimum verification score filter (0 means no gating).
    :param exclude_unexported: If True, exclude contacts previously exported via ExportHistory.
    :param destination_prefix: Destination prefix string to filter in ExportHistory.
    :param only_classified: If True, only include contacts with explicit phone classification (Classified_*).
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
            .where(ExportHistory.destination.like(f"{destination_prefix}%"))
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

    logger.info("Sorting %d prioritized contacts into 12 Saleshandy permutations (min_score=%d, only_classified=%s)...", len(prioritized_contacts), min_score, only_classified)

    buckets: dict[str, list[dict]] = {
        f"{trade}_{persona}_{phone}": []
        for trade in ("HVAC", "Plumbing")
        for persona in ("Owner", "NonOwner")
        for phone in ("IVR", "Receptionist", "Voicemail")
    }

    for contact, business, persona in prioritized_contacts:
        if only_classified and not (contact.lead_status and "Classified" in contact.lead_status):
            continue

        trade = classify_trade(business)
        phone_type = classify_phone_type(contact, business=business)

        # --- Pre-Export Trade Sanity Auditor & Auto-Healer ---
        comp_raw_name = (business.business_name or "").strip()
        comp_hvac = bool(HVAC_RE.search(comp_raw_name))
        comp_plumb = bool(PLUMBING_RE.search(comp_raw_name))

        # Check for trade contradiction against explicit business name
        if trade == "HVAC" and comp_plumb and not comp_hvac:
            logger.warning(
                "Trade Sanity Guardrail: Auto-corrected trade from HVAC to Plumbing for '%s' (Contact ID: %d, Email: %s)",
                comp_raw_name, contact.id, contact.email
            )
            trade = "Plumbing"
        elif trade == "Plumbing" and comp_hvac and not comp_plumb:
            logger.warning(
                "Trade Sanity Guardrail: Auto-corrected trade from Plumbing to HVAC for '%s' (Contact ID: %d, Email: %s)",
                comp_raw_name, contact.id, contact.email
            )
            trade = "HVAC"

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

        buckets = sort_database_into_12_buckets(
            session,
            min_score=min_score,
            exclude_unexported=exclude_unexported,
            destination_prefix="saleshandy_csv"
        )
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
                        destination=f"saleshandy_csv_{perm_tag.lower()}",
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


def push_to_saleshandy_api(min_score: int = 80, exclude_unexported: bool = False, only_classified: bool = False) -> dict[str, int]:
    """
    Push sorted lead buckets directly into live Saleshandy campaign sequences via API.
    
    Defaults to min_score=80 (Verified Safe Only) to protect domain sender reputation
    and prevent bounces from unverified pattern guesses.
    """
    if not SALESHANDY_API_KEY:
        logger.warning("SALESHANDY_API_KEY is not set in environment; skipping API push.")
        return {}

    if min_score < 50:
        logger.warning(
            "SAFETY WARNING: push_to_saleshandy_api invoked with min_score=%d (<50). "
            "Unverified/unknown email addresses may bounce and hurt sender reputation.",
            min_score
        )

    from datetime import datetime, timezone
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from app.db.create_tables import ExportHistory

    session_db = Session()
    try:
        buckets = sort_database_into_12_buckets(
            session_db,
            min_score=min_score,
            exclude_unexported=exclude_unexported,
            destination_prefix="saleshandy_api",
            only_classified=only_classified
        )
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

        # Step 1: Pre-fetch live sequence Step 1 IDs
        seq_step_map = {}
        try:
            seq_resp = http_session.get("https://open-api.saleshandy.com/v1/sequences", headers=headers, timeout=15)
            if seq_resp.status_code == 200:
                payload = seq_resp.json().get("payload", [])
                for s in payload:
                    if s.get("id") and s.get("steps"):
                        seq_step_map[s["id"]] = s["steps"][0]["id"]
        except Exception as e:
            logger.warning("Could not pre-fetch Saleshandy sequence steps: %s", e)

        # Step 1b: Dynamic System Field Discovery with exact Saleshandy defaults
        field_id_map = {
            "email": "qPBRX3lBPD",
            "firstName": "KwO0O59MP6",
            "lastName": "VaDq03egzo",
            "company": "gw4lvyOvwA",
            "phoneNumber": "gw4lvyOLwA",
            "jobTitle": "lwGMlr4Va6",
            "website": "DPQGBN12zR",
        }
        try:
            field_resp = http_session.get("https://open-api.saleshandy.com/v1/fields?systemFields=true", headers=headers, timeout=15)
            if field_resp.status_code == 200:
                fields_payload = field_resp.json().get("payload", [])
                for f in fields_payload:
                    mdf = f.get("mappingDefaultField")
                    if mdf and f.get("id"):
                        field_id_map[mdf] = f["id"]
        except Exception as fe:
            logger.warning("Could not dynamically fetch Saleshandy system fields (using defaults): %s", fe)

        # Step 2: Import prospects into target sequences
        for perm_tag, records in buckets.items():
            seq_id = SEQUENCE_ID_MAP.get(perm_tag)
            if not seq_id:
                logger.warning("No sequence ID mapped in .env for permutation %s (set SH_SEQ_%s)", perm_tag, perm_tag.upper())
                results[perm_tag] = 0
                continue

            step_id = seq_step_map.get(seq_id)
            if not step_id:
                logger.warning("No Step 1 ID found for Saleshandy sequence %s", seq_id)
                results[perm_tag] = 0
                continue

            prospect_list = []
            rec_map = {}

            for rec in records:
                email = (rec.get("Email") or "").strip()
                if not email:
                    continue

                first_name = (rec.get("First Name") or "Team").replace("/", " ").strip()
                last_name = (rec.get("Last Name") or "Team").replace("/", " ").strip()
                company = (rec.get("Company") or "Business").replace("$", "").replace("/", " ").strip()
                phone = (rec.get("Phone") or "").strip()
                job_title = (rec.get("Job Title") or "").strip()
                website = (rec.get("Website") or "").strip()

                prospect_fields = [
                    {"id": field_id_map.get("email", "qPBRX3lBPD"), "value": email},
                    {"id": field_id_map.get("firstName", "KwO0O59MP6"), "value": first_name if first_name else "Team"},
                    {"id": field_id_map.get("lastName", "VaDq03egzo"), "value": last_name if last_name else "Team"},
                    {"id": field_id_map.get("company", "gw4lvyOvwA"), "value": company if company else "Business"},
                    {"id": field_id_map.get("phoneNumber", "gw4lvyOLwA"), "value": phone},
                    {"id": field_id_map.get("jobTitle", "lwGMlr4Va6"), "value": job_title},
                ]
                if website and "website" in field_id_map:
                    prospect_fields.append({"id": field_id_map["website"], "value": website})

                prospect_list.append({"fields": prospect_fields})
                rec_map[email] = rec

            pushed_count = 0
            if prospect_list:
                import_url = "https://open-api.saleshandy.com/v1/prospects/import"
                payload = {
                    "prospectList": prospect_list,
                    "stepId": step_id,
                    "conflictAction": "overwrite",
                    "verifyProspects": False
                }

                try:
                    resp = http_session.post(import_url, json=payload, headers=headers, timeout=20)
                    if resp.status_code in (200, 201):
                        pushed_count = len(prospect_list)
                        for rec in records:
                            cid = rec.get("Contact ID")
                            if cid:
                                session_db.add(ExportHistory(
                                    contact_id=cid,
                                    destination=f"saleshandy_api_{perm_tag.lower()}",
                                    exported_at=datetime.now(timezone.utc)
                                ))
                    else:
                        logger.warning("Saleshandy API prospect import failed for seq %s (%s): HTTP %d — %s", seq_id, perm_tag, resp.status_code, resp.text[:200])
                except Exception as e:
                    logger.error("Error importing prospects into Saleshandy sequence %s: %s", seq_id, e)

            session_db.commit()
            logger.info("Successfully imported %d prospects into Saleshandy sequence %s (%s)", pushed_count, seq_id, perm_tag)
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
