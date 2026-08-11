"""
Call Recording Downloader & Manager for Twilio Outbound Classification Calls.

Downloads MP3 audio files for completed classification calls and saves them in data/call_recordings/
named by business name, phone number, classification status, and Call SID.
"""

import os
import re
import logging
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path("data/call_recordings")


def download_call_recording(call_sid: str, business_name: str | None, phone: str | None, classification: str | None) -> str | None:
    """
    Fetch MP3 recording from Twilio for a given call_sid and save it locally.
    
    File naming format:
    data/call_recordings/{sanitized_business_name}_{phone}_{classification}_{call_sid}.mp3
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token or not call_sid:
        return None

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    clean_name = re.sub(r'[^a-zA-Z0-9]+', '_', (business_name or "unknown_business").strip()).strip('_').lower()
    clean_phone = re.sub(r'[^0-9+]', '', (phone or "").strip())
    clean_status = (classification or "Unclassified").strip()

    file_prefix = f"{clean_name}_{clean_phone}_{clean_status}_{call_sid}"
    target_path = RECORDINGS_DIR / f"{file_prefix}.mp3"

    # Skip if already downloaded
    if target_path.exists():
        return str(target_path)

    # Query Twilio Call Recordings API for this call
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}/Recordings.json"
    try:
        resp = requests.get(url, auth=(account_sid, auth_token), timeout=10)
        if resp.status_code != 200:
            return None

        recordings = resp.json().get("recordings", [])
        if not recordings:
            return None

        recording_sid = recordings[0].get("sid")
        if not recording_sid:
            return None

        # Fetch MP3 audio file
        audio_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"
        audio_resp = requests.get(audio_url, auth=(account_sid, auth_token), timeout=15)

        if audio_resp.status_code == 200 and len(audio_resp.content) > 0:
            with open(target_path, "wb") as f:
                f.write(audio_resp.content)
            logger.info("Saved call recording MP3 to: %s", target_path)
            return str(target_path)
    except Exception as e:
        logger.warning("Failed to download recording for call %s: %s", call_sid, e)

    return None
