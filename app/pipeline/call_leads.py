"""
Twilio Phone Classifier Outbound Calling Handler.

Queries verified contacts with phone numbers and dispatches calls to classify 
the destination as IVR, Receptionist, Voicemail, or Disconnected line.
"""

from __future__ import annotations
import logging
import os
import time
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
            contact_time = getattr(contact, "created_at", None)
            # Only reconcile to fallback Voicemail if contact_time exists AND is older than cutoff threshold
            if contact_time is not None:
                if (contact_time.tzinfo is None and contact_time < cutoff.replace(tzinfo=None)) or (contact_time.tzinfo is not None and contact_time < cutoff):
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


def trigger_twilio_outbound_calls(min_score: int = 80, limit: int | None = None, twiml_url: str | None = None) -> int:
    """
    Dispatch classification calls to determine if destination connects to:
      - IVR (Phone Tree)
      - Receptionist (Live Human / Front Desk)
      - Voicemail (Answering Machine)
      - Disconnected / Invalid Line

    Ensures EXACTLY ONE CALL PER BUSINESS / PHONE NUMBER.

    :param min_score: Minimum verification score filter (defaults to 80 for Safe Only).
    :param limit: Max number of calls to dispatch (None for unlimited).
    :param twiml_url: URL that returns TwiML classification instructions for the call.
    :return: Count of successfully dispatched calls.
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("Skipping Twilio phone classification: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN missing in .env")
        return 0

    reconcile_stale_phone_classifications(timeout_hours=2.0)
    sync_phone_classifications_across_business_contacts()

    from app.db.create_tables import Contact, Business, EmailVerification

    session = Session()
    try:
        # Find contacts with phone numbers that haven't been classified yet (or need retry)
        unclassified_statuses = ("Not Contacted", "Verified", "Unknown", "Unanswered_Retry", None)
        query = (
            session.query(Contact, Business)
            .join(Business, Contact.business_id == Business.id)
            .filter(Contact.email.isnot(None))
            .filter(Contact.email != "")
            .filter((Business.phone.isnot(None) & (Business.phone != "")) | (Contact.phone.isnot(None) & (Contact.phone != "")))
            .filter(
                (Contact.lead_status.in_(("Not Contacted", "Verified", "Unknown", "Unanswered_Retry"))) |
                (Contact.lead_status.is_(None))
            )
            .filter((Contact.call_attempts.is_(None)) | (Contact.call_attempts < 3))
        )

        if min_score > 0:
            query = query.join(EmailVerification, EmailVerification.contact_id == Contact.id).filter(EmailVerification.score >= min_score)

        if limit:
            query = query.limit(limit)

        target_contacts = query.all()

        logger.info("Found %d contacts ready for phone number classification...", len(target_contacts))
        dispatched_count = 0
        dispatched_businesses: set[int] = set()
        dispatched_phones: set[str] = set()

        twilio_api_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json"

        for contact, business in target_contacts:
            phone_to_call = (business.phone or contact.phone or "").strip()
            if not phone_to_call:
                continue

            # Skip redundant calls if this business or business phone number was already dispatched in this run
            if business.id in dispatched_businesses or phone_to_call in dispatched_phones:
                continue

            dispatched_businesses.add(business.id)
            dispatched_phones.add(phone_to_call)

            # Form parameters for Twilio AMD & Phone Classifier (Tuned for Front Desk & Receptionist detection)
            data = {
                "To": phone_to_call,
                "From": TWILIO_FROM_NUMBER,
                "Twiml": "<Response><Pause length=\"12\"/><Hangup/></Response>",
                # Enable Advanced Twilio Answering Machine & Call Classifier Detection
                "MachineDetection": "DetectMessageEnd",
                "MachineDetectionTimeout": "10",
                "MachineWordsThreshold": "12",
                "SpeechThreshold": "4500",
                "SilenceThreshold": "800",
                # Enable call recording
                "Record": "true",
                "RecordingChannels": "single",
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
                    
                    # Update status & increment call attempts for ALL contacts at this business
                    sibling_query = session.query(Contact).filter(
                        Contact.business_id == business.id,
                        Contact.lead_status.in_(unclassified_statuses)
                    )

                    for sib in sibling_query.all():
                        sib.lead_status = "Pending_Classification"
                        sib.call_attempts = (sib.call_attempts or 0) + 1

                    dispatched_count += 1
                    logger.info(
                        "Dispatched main business classification call to %s (%s) — Call SID: %s",
                        phone_to_call, business.business_name, call_sid
                    )
                    # Respect Twilio concurrency & CPS limits to avoid Error 10004
                    time.sleep(1.2)
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


def poll_and_classify_completed_calls(wait_for_completion: bool = True, max_wait_sec: int = 45) -> dict[str, int]:
    """
    Poll recent Twilio calls, download WAV recordings, transcribe audio via STT,
    and update database contacts with accurate classification statuses:
      - Classified_Receptionist
      - Classified_IVR
      - Classified_Voicemail
      - Classified_Disconnected
      - Unanswered_Retry
    """
    from app.pipeline.classify_call import fetch_and_classify_twilio_recording
    from app.pipeline.call_recordings import download_call_recording
    from app.db.create_tables import Contact, Business

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("Twilio credentials missing. Skipping call classification polling.")
        return {}

    start_time = time.time()
    if wait_for_completion:
        logger.info("Waiting %d seconds for in-flight calls to connect/record...", min(15, max_wait_sec))
        time.sleep(min(15, max_wait_sec))

    session = Session()
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json?PageSize=100"
        calls = []
        while url:
            resp = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
            if resp.status_code != 200:
                logger.error("Failed to query Twilio calls API: HTTP %d", resp.status_code)
                break
            payload = resp.json()
            calls.extend(payload.get("calls", []))
            next_uri = payload.get("next_page_uri")
            if next_uri and len(calls) < 400:
                url = f"https://api.twilio.com{next_uri}"
            else:
                break

        classified_counts: dict[str, int] = {}

        for call in calls:
            to_num = call.get("to")
            status = call.get("status")
            call_sid = call.get("sid")
            duration = float(call.get("duration") or 0.0)
            error_code = call.get("error_code")

            if status in ("completed", "no-answer", "busy", "failed", "canceled"):
                classified_status = None
                
                if status == "completed":
                    rec_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}/Recordings.json"
                    try:
                        r_resp = requests.get(rec_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
                        if r_resp.status_code == 200:
                            recs = r_resp.json().get("recordings", [])
                            if recs:
                                rec_sid = recs[0].get("sid")
                                classified_status, transcript = fetch_and_classify_twilio_recording(rec_sid, duration_sec=duration)
                                logger.info("STT Classified Call %s (%s) -> %s [\"%s\"]", call_sid, to_num, classified_status, transcript[:60])
                    except Exception as e:
                        logger.warning("Error fetching recording for call %s: %s", call_sid, e)

                if not classified_status:
                    if status == "failed" and error_code in ("21211", "13223", "13224", "13225"):
                        classified_status = "Classified_Disconnected"
                    elif status in ("no-answer", "busy", "canceled"):
                        classified_status = "Unanswered_Retry"
                    elif status == "failed":
                        classified_status = "Unanswered_Retry"
                    else:
                        classified_status = "Classified_Voicemail"

                contact_pairs = session.query(Contact, Business).join(Business, Contact.business_id == Business.id).filter((Contact.phone == to_num) | (Business.phone == to_num)).all()
                biz_name = contact_pairs[0][1].business_name if contact_pairs else "unknown_business"

                for c, b in contact_pairs:
                    if classified_status == "Unanswered_Retry":
                        if (getattr(c, "call_attempts", 0) or 0) >= 3:
                            c.lead_status = "Classified_Voicemail"
                        else:
                            c.lead_status = "Unanswered_Retry"
                    else:
                        c.lead_status = classified_status

                if call_sid:
                    final_status = contact_pairs[0][0].lead_status if contact_pairs else classified_status
                    download_call_recording(call_sid, biz_name, to_num, final_status)

                classified_counts[classified_status] = classified_counts.get(classified_status, 0) + 1

        session.commit()
        sync_phone_classifications_across_business_contacts(session=session)
        logger.info("Call polling & STT classification complete: %s", classified_counts)
        return classified_counts
    except Exception as e:
        session.rollback()
        logger.error("Error polling/classifying calls: %s", e)
        return {}
    finally:
        session.close()


if __name__ == "__main__":
    from app.logging_config import setup_logging
    setup_logging()
    trigger_twilio_outbound_calls()
    poll_and_classify_completed_calls(wait_for_completion=True)

