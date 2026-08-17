"""
Audio Transcription & Speech-to-Text Call Classifier.

Transcribes call audio recordings and classifies phone destinations into:
  - Classified_Receptionist  (Live human front desk / office staff)
  - Classified_IVR           (Automated phone tree / queue / menu options)
  - Classified_Voicemail     (Answering machine / leave a message after tone)
  - Classified_Disconnected  (Carrier SIT tones / out of service / invalid number)
"""

from __future__ import annotations
import io
import logging
import os
import re
import requests
import speech_recognition as sr
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

IVR_KEYWORDS = [
    "press 1", "press one", "press 2", "press two", "press 3", "press three",
    "press 4", "press four", "press 0", "press zero", "press *", "press #",
    "menu", "option", "options", "to speak with", "to schedule", "for service",
    "for billing", "for emergency", "for sales", "for dispatch", "for repairs",
    "dial", "extension", "directory", "quality assurance", "training purposes",
    "monitored", "recorded", "stay on the line", "transfer you", "call may be",
    "please listen", "following options", "representative will be"
]

VM_KEYWORDS = [
    "leave a message", "leave your name", "leave a brief message", "after the tone",
    "after the beep", "at the tone", "at the beep", "unable to take your call",
    "sorry we missed you", "we are unavailable", "mailbox is full", "mailbox",
    "voicemail", "please call back", "not available right now", "reached the voicemail",
    "leave detailed message", "return your call"
]

RECEPTIONIST_KEYWORDS = [
    "this is", "my name is", "how can i help", "how may i help",
    "good morning", "good afternoon", "how can i direct", "thanks for calling",
    "thank you for calling", "speaking", "hello hi", "can you hear me", "hold on one second"
]

DISCONNECTED_KEYWORDS = [
    "not in service", "no longer in service", "disconnected", "cannot be completed",
    "check the number", "unallocated", "is not reachable", "temporarily unavailable",
    "invalid number", "call cannot be completed as dialed"
]


def classify_audio_transcript(transcript: str, duration_sec: float = 0.0) -> str:
    """
    Classify a call based on speech-to-text transcript and call duration.
    
    Returns one of:
      - 'Classified_Receptionist'
      - 'Classified_IVR'
      - 'Classified_Voicemail'
      - 'Classified_Disconnected'
    """
    clean_text = (transcript or "").lower().strip()

    if not clean_text or "[unclear" in clean_text or "[no audio" in clean_text:
        if duration_sec > 0 and duration_sec <= 4.0:
            return "Classified_Disconnected"
        return "Classified_Voicemail"

    has_disc = any(k in clean_text for k in DISCONNECTED_KEYWORDS)
    has_ivr = any(k in clean_text for k in IVR_KEYWORDS)
    has_vm = any(k in clean_text for k in VM_KEYWORDS)
    has_rec = any(k in clean_text for k in RECEPTIONIST_KEYWORDS)

    if has_disc:
        return "Classified_Disconnected"

    if has_ivr:
        return "Classified_IVR"

    if has_rec and not has_vm:
        return "Classified_Receptionist"

    if has_vm and not has_rec:
        return "Classified_Voicemail"

    if has_rec and has_vm:
        # Mixed keywords: If human greeting + voicemail indicators ("this is John, please leave a message")
        return "Classified_Voicemail"

    # Fallback heuristic: Long continuous speech without pause is usually an IVR/announcement
    if len(clean_text.split()) > 14:
        return "Classified_IVR"

    return "Classified_Voicemail"


def transcribe_audio_bytes(audio_bytes: bytes, duration_sec: float = 0.0) -> tuple[str, str]:
    """
    Transcribe WAV audio bytes using SpeechRecognition and classify the outcome.
    
    :return: (classified_status, transcript_text)
    """
    if not audio_bytes or len(audio_bytes) < 1000:
        return ("Classified_Disconnected" if duration_sec <= 4.0 else "Classified_Voicemail", "[No Audio Content]")

    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(io.BytesIO(audio_bytes)) as source:
            audio = recognizer.record(source)
        transcript = recognizer.recognize_google(audio).lower()
        classification = classify_audio_transcript(transcript, duration_sec=duration_sec)
        return (classification, transcript)
    except sr.UnknownValueError:
        classification = "Classified_Disconnected" if duration_sec <= 4.0 else "Classified_Voicemail"
        return (classification, "[Unclear / Silence / SIT Tone]")
    except Exception as e:
        logger.warning("Error transcribing audio: %s", e)
        return ("Classified_Voicemail", f"[Transcription Error: {e}]")


def fetch_and_classify_twilio_recording(recording_sid: str, duration_sec: float = 0.0) -> tuple[str, str]:
    """
    Fetch WAV audio for a Twilio recording SID and classify it.
    
    :return: (classified_status, transcript_text)
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not recording_sid:
        return ("Classified_Voicemail", "")

    wav_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Recordings/{recording_sid}.wav"
    try:
        resp = requests.get(wav_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=20)
        if resp.status_code == 200 and len(resp.content) > 1000:
            return transcribe_audio_bytes(resp.content, duration_sec=duration_sec)
        else:
            logger.warning("Twilio WAV download returned status %d for recording %s", resp.status_code, recording_sid)
    except Exception as e:
        logger.error("Failed to fetch/classify recording %s: %s", recording_sid, e)

    return ("Classified_Voicemail", "")
