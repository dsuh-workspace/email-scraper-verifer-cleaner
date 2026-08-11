"""
Download all available Twilio MP3 call recordings for database businesses.

Files are saved into data/call_recordings/ named:
{sanitized_business_name}_{phone}_{classification}_{call_sid}.mp3
"""

import os
import sys
import requests
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import Contact, Business
from app.pipeline.call_recordings import download_call_recording

load_dotenv()
Session = sessionmaker(bind=engine)


def sync_all_recordings():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        print("Twilio credentials missing.")
        return

    print("Fetching Twilio recordings metadata...")
    rec_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings.json?PageSize=100"
    recordings = []
    
    while rec_url and len(recordings) < 500:
        resp = requests.get(rec_url, auth=(account_sid, auth_token), timeout=15)
        if resp.status_code != 200:
            break
        data = resp.json()
        recordings.extend(data.get("recordings", []))
        next_page = data.get("next_page_uri")
        rec_url = f"https://api.twilio.com{next_page}" if next_page else None

    print(f"Found {len(recordings)} total recordings on Twilio account.")

    session = Session()
    downloaded_count = 0
    try:
        for rec in recordings:
            call_sid = rec.get("call_sid")
            rec_sid = rec.get("sid")
            if not call_sid or not rec_sid:
                continue

            # Query call details to find 'to' phone number
            call_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
            call_resp = requests.get(call_url, auth=(account_sid, auth_token), timeout=10)
            if call_resp.status_code != 200:
                continue

            call_data = call_resp.json()
            to_phone = call_data.get("to", "")

            # Query database for business name and status
            c_pair = (
                session.query(Contact, Business)
                .join(Business, Contact.business_id == Business.id)
                .filter(Contact.phone == to_phone)
                .first()
            )

            biz_name = c_pair[1].business_name if c_pair else "unknown_business"
            status = c_pair[0].lead_status if c_pair else "Classified_Call"

            saved_path = download_call_recording(call_sid, biz_name, to_phone, status)
            if saved_path:
                downloaded_count += 1
                print(f"  [{downloaded_count}] Downloaded MP3 -> {saved_path}")

        print(f"\nFinished downloading {downloaded_count} MP3 recordings to data/call_recordings/")

    finally:
        session.close()


if __name__ == "__main__":
    sync_all_recordings()
