"""
Twilio Phone Classifier Outbound Calling Handler.

Queries verified contacts with phone numbers and dispatches calls to classify 
the destination as IVR, Receptionist, Voicemail, or Disconnected line.
"""

from __future__ import annotations
import logging
import os
import requests
from dotenv import load_dotenv
from sqlalchemy.orm import sessionmaker

from app.db.database import engine

logger = logging.getLogger(__name__)
load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "+14085029426")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://daniel-phone-classifier.autopilotlocal.com")

Session = sessionmaker(bind=engine)


def reconcile_stale_phone_classifications(session=None, timeout_hours: float = 2.0) -> int:
    """
    Reconcile contacts stuck in 'Pending_Classification' due to dropped callbacks or timed out calls.
    Resets status to 'Voicemail' fallback if pending longer than timeout_hours.
    """
    from datetime import datetime, timezone, timedelta
    from app.db.create_tables import Contact

    own_session = False
    if session is None:
        session = Session()
        own_session = True

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=timeout_hours)
        pending_contacts = session.query(Contact).filter(Contact.lead_status == "Pending_Classification").all()

        reconciled_count = 0
        for contact in pending_contacts:
            # Check last_crawled_at / updated_at or fallback if older than cutoff
            contact_time = contact.last_crawled_at
            if contact_time is None or (contact_time.tzinfo is None and contact_time < cutoff.replace(tzinfo=None)) or (contact_time.tzinfo is not None and contact_time < cutoff):
                contact.lead_status = "Voicemail"
                reconciled_count += 1

        if own_session and reconciled_count > 0:
            session.commit()
            logger.info("Reconciled %d stale 'Pending_Classification' contacts to 'Voicemail'", reconciled_count)
        return reconciled_count
    except Exception as e:
        if own_session:
            session.rollback()
        logger.error("Error reconciling stale phone classifications: %s", e)
        return 0
    finally:
        if own_session:
            session.close()


def sync_phone_classifications_across_business_contacts(session=None) -> int:
    """
    Propagate classification status across all contacts belonging to the same business or sharing the same phone number.
    If one contact at a business has been classified (e.g. IVR, Receptionist, Voicemail, Disconnected),
    all other unclassified contacts at that business inherit the status without needing another call.
    """
    from app.db.create_tables import Contact

    own_session = False
    if session is None:
        session = Session()
        own_session = True

    try:
        # Fetch all classified contacts
        classified_contacts = (
            session.query(Contact)
            .filter(Contact.lead_status.isnot(None))
            .filter(Contact.lead_status.notin_(("Not Contacted", "Verified", "Pending_Classification")))
            .all()
        )

        synced_count = 0
        for cc in classified_contacts:
            status = cc.lead_status
            if not cc.business_id and not cc.phone:
                continue

            # Query sibling unclassified contacts at same business or with same phone
            query = session.query(Contact).filter(
                Contact.id != cc.id,
                Contact.lead_status.in_(("Not Contacted", "Verified", "Pending_Classification"))
            )
            if cc.business_id:
                query = query.filter(Contact.business_id == cc.business_id)
            elif cc.phone:
                query = query.filter(Contact.phone == cc.phone)

            siblings = query.all()
            for sibling in siblings:
                sibling.lead_status = status
                synced_count += 1

        if own_session and synced_count > 0:
            session.commit()
            logger.info("Propagated phone classification status to %d sibling contacts across businesses", synced_count)
        return synced_count
    except Exception as e:
        if own_session:
            session.rollback()
        logger.error("Error propagating phone classification across contacts: %s", e)
        return 0
    finally:
        if own_session:
            session.close()


def trigger_twilio_outbound_calls(min_score: int = 50, limit: int = 50, twiml_url: str | None = None) -> int:
    """
    Dispatch classification calls to determine if destination connects to:
      - IVR (Phone Tree)
      - Receptionist (Live Human / Front Desk)
      - Voicemail (Answering Machine)
      - Disconnected / Invalid Line

    Ensures EXACTLY ONE CALL PER BUSINESS / PHONE NUMBER.

    :param min_score: Minimum verification score filter.
    :param limit: Max number of calls to dispatch.
    :param twiml_url: URL that returns TwiML classification instructions for the call.
    :return: Count of successfully dispatched calls.
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("Skipping Twilio phone classification: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN missing in .env")
        return 0

    reconcile_stale_phone_classifications(timeout_hours=2.0)
    sync_phone_classifications_across_business_contacts()

    base_url_clean = PUBLIC_BASE_URL.rstrip('/') if PUBLIC_BASE_URL else "https://daniel-phone-classifier.autopilotlocal.com"
    
    if not twiml_url:
        twiml_url = f"{base_url_clean}/outbound-call"

    amd_callback_url = f"{base_url_clean}/amd-callback"

    from app.db.create_tables import Contact, Business

    session = Session()
    try:
        # Find contacts with phone numbers that haven't been classified yet
        unclassified_statuses = ("Not Contacted", "Verified")
        target_contacts = (
            session.query(Contact, Business)
            .join(Business, Contact.business_id == Business.id)
            .filter(Contact.phone.isnot(None))
            .filter(Contact.phone != "")
            .filter(Contact.lead_status.in_(unclassified_statuses))
            .limit(limit)
            .all()
        )

        logger.info("Found %d contacts ready for phone number classification...", len(target_contacts))
        dispatched_count = 0
        dispatched_phones: set[str] = set()

        twilio_api_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json"

        for contact, business in target_contacts:
            phone_to_call = contact.phone.strip()

            # Skip redundant calls if this phone number or business was already dispatched in this run
            if phone_to_call in dispatched_phones:
                continue

            dispatched_phones.add(phone_to_call)

            # Form parameters for Twilio Async AMD & Phone Classifier
            data = {
                "To": phone_to_call,
                "From": TWILIO_FROM_NUMBER,
                "Url": twiml_url,
                # Enable Twilio Answering Machine & Call Classifier Detection
                "MachineDetection": "Enable",
                "AsyncAmd": "true",
                "AsyncAmdStatusCallback": amd_callback_url,
                "MachineDetectionTimeout": "5",
            }

            try:
                response = requests.post(
                    twilio_api_url,
                    data=data,
                    auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                    timeout=15,
                )

                if response.status_code in (200, 201):
                    call_data = response.json()
                    call_sid = call_data.get("sid")
                    
                    # Update status for ALL contacts at this business / sharing this phone number to indicate call in progress
                    sibling_query = session.query(Contact).filter(
                        Contact.lead_status.in_(unclassified_statuses)
                    )
                    if contact.business_id:
                        sibling_query = sibling_query.filter(Contact.business_id == contact.business_id)
                    else:
                        sibling_query = sibling_query.filter(Contact.phone == phone_to_call)

                    for sib in sibling_query.all():
                        sib.lead_status = "Pending_Classification"

                    dispatched_count += 1
                    logger.info(
                        "Dispatched single classification call to %s (%s) — Call SID: %s",
                        phone_to_call, business.business_name, call_sid
                    )
                else:
                    logger.warning(
                        "Twilio classification call failed for %s (%s): HTTP %d — %s",
                        phone_to_call, business.business_name, response.status_code, response.text[:200]
                    )
            except Exception as e:
                logger.error("Error triggering classification call to %s: %s", phone_to_call, e)

        session.commit()
        logger.info("Twilio phone classification stage complete. Dispatched %d calls.", dispatched_count)
        return dispatched_count
    except Exception as e:
        session.rollback()
        logger.error("Error in Twilio phone classification stage: %s", e)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    from app.logging_config import setup_logging
    setup_logging()
    trigger_twilio_outbound_calls()
