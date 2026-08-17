"""
Polls Twilio REST API to check the live status and performs Speech-to-Text audio classification.
Updates database/hvac_leads.db as calls complete.
"""

import os
import sys
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import Contact, Business
from app.pipeline.call_recordings import download_call_recording
from app.pipeline.classify_call import fetch_and_classify_twilio_recording

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

Session = sessionmaker(bind=engine)


def check_twilio_calls():
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("Twilio credentials missing.")
        return

    calls = []
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json?PageSize=100"
    
    while url and len(calls) < 1000:
        resp = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
        if resp.status_code != 200:
            print(f"Twilio API check failed: HTTP {resp.status_code}")
            break

        data = resp.json()
        calls.extend(data.get("calls", []))
        next_page = data.get("next_page_uri")
        if next_page:
            url = f"https://api.twilio.com{next_page}"
        else:
            break

    print(f"Fetched {len(calls)} total recent Twilio call records across pages.")

    completed_count = 0
    in_progress_count = 0
    classified_counts = {}

    session = Session()
    try:
        for call in calls:
            to_num = call.get("to")
            status = call.get("status")
            call_sid = call.get("sid")
            duration = float(call.get("duration") or 0.0)
            error_code = call.get("error_code")

            if status in ("completed", "no-answer", "busy", "failed", "canceled"):
                completed_count += 1
                
                # Check for audio recording if call completed
                classified_status = None
                transcript = ""
                
                if status == "completed":
                    # Query Twilio Call Recordings API
                    rec_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}/Recordings.json"
                    try:
                        r_resp = requests.get(rec_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
                        if r_resp.status_code == 200:
                            recs = r_resp.json().get("recordings", [])
                            if recs:
                                rec_sid = recs[0].get("sid")
                                classified_status, transcript = fetch_and_classify_twilio_recording(rec_sid, duration_sec=duration)
                    except Exception as e:
                        print(f"Recording fetch error for {call_sid}: {e}")

                if not classified_status:
                    if status == "failed" and error_code in ("21211", "13223", "13224", "13225"):
                        classified_status = "Classified_Disconnected"
                    elif status in ("no-answer", "busy", "canceled"):
                        classified_status = "Unanswered_Retry"
                    elif status == "failed":
                        # If rate limited (10004) or carrier unallocated, retry
                        classified_status = "Unanswered_Retry"
                    else:
                        classified_status = "Classified_Voicemail"

                # Update database & download MP3 call recording
                contact_pairs = session.query(Contact, Business).join(Business, Contact.business_id == Business.id).filter(Contact.phone == to_num).all()
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
            else:
                in_progress_count += 1

        session.commit()

        pending_in_db = session.query(Contact).filter(Contact.lead_status == "Pending_Classification").count()
        classified_in_db = session.query(Contact).filter(Contact.lead_status.like("Classified_%")).count()

        print(f"\nTwilio Calls Progress -> Completed: {completed_count} | In Progress: {in_progress_count}")
        print("Classification Breakdown:")
        for k, v in classified_counts.items():
            print(f"  • {k}: {v}")
        print(f"\nDatabase Lead Status -> Classified: {classified_in_db} | Pending: {pending_in_db}")

        return classified_in_db, pending_in_db

    finally:
        session.close()


if __name__ == "__main__":
    check_twilio_calls()
