"""
Polls Twilio REST API to check the live status and AMD classification of the 50 dispatched calls.
Updates database/hvac_leads.db as calls complete.
"""

import os
import time
import requests
from dotenv import load_dotenv
from sqlalchemy.orm import sessionmaker

from app.db.database import engine
from app.db.create_tables import Contact

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

Session = sessionmaker(bind=engine)


def check_twilio_calls():
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("Twilio credentials missing.")
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json?PageSize=100"
    resp = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
    
    if resp.status_code != 200:
        print(f"Twilio API check failed: HTTP {resp.status_code}")
        return

    calls = resp.json().get("calls", [])
    print(f"Fetched {len(calls)} recent Twilio call records.")

    completed_count = 0
    in_progress_count = 0

    session = Session()
    try:
        for call in calls:
            to_num = call.get("to")
            status = call.get("status")
            answered_by = call.get("answered_by")

            if status in ("completed", "no-answer", "busy", "failed", "canceled"):
                completed_count += 1
                
                # Determine classification status based on Twilio AMD / Audio response
                if answered_by == "machine_end_beep" or answered_by == "machine_start":
                    classified_status = "Classified_Voicemail"
                elif answered_by == "human":
                    classified_status = "Classified_Receptionist"
                elif status in ("no-answer", "busy", "failed", "canceled"):
                    classified_status = "Classified_Disconnected"
                else:
                    classified_status = "Classified_Voicemail"

                # Update database
                contacts = session.query(Contact).filter(Contact.phone == to_num).all()
                for c in contacts:
                    if c.lead_status in ("Pending_Classification", "Not Contacted"):
                        c.lead_status = classified_status
            else:
                in_progress_count += 1

        session.commit()

        pending_in_db = session.query(Contact).filter(Contact.lead_status == "Pending_Classification").count()
        classified_in_db = session.query(Contact).filter(Contact.lead_status.like("Classified_%")).count()

        print(f"Twilio Calls Progress -> Completed: {completed_count} | In Progress: {in_progress_count}")
        print(f"Database Lead Status -> Classified: {classified_in_db} | Pending: {pending_in_db}")

        return classified_in_db, pending_in_db

    finally:
        session.close()


if __name__ == "__main__":
    check_twilio_calls()
