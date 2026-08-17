import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
from app.logging_config import setup_logging
from app.pipeline.call_leads import trigger_twilio_outbound_calls, poll_and_classify_completed_calls
from app.db.database import engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

setup_logging()

print("=" * 65)
print("STARTING LIVE TWILIO OUTBOUND PHONE CALLING & STT CLASSIFIER")
print("=" * 65)

# Step 1: Dispatch calls to all safe uncalled businesses
print("\n[Phase 1] Dispatching Twilio outbound phone calls to 100% Safe businesses...")
dispatched = trigger_twilio_outbound_calls(min_score=80, limit=None)
print(f"Dispatched {dispatched} live outbound phone calls via Twilio.")

if dispatched == 0:
    print("No pending calls to dispatch.")
    sys.exit(0)

# Step 2: Poll and transcribe recordings in batches
print("\n[Phase 2] Waiting for calls to complete and running Speech-to-Text transcription...")
time.sleep(25)  # Wait for calls to ring, connect, and record 12s

total_classified = {}
for poll_round in range(1, 4):
    print(f"\n--- Polling & Transcribing Round {poll_round}/3 ---")
    counts = poll_and_classify_completed_calls(wait_for_completion=True, max_wait_sec=20)
    for k, v in counts.items():
        total_classified[k] = total_classified.get(k, 0) + v
    time.sleep(10)

print("\n" + "=" * 65)
print("PHONE CALLING & AUDIO CLASSIFICATION RESULTS")
print("=" * 65)
for status, count in sorted(total_classified.items(), key=lambda x: x[1], reverse=True):
    print(f"  • {status:<30}: {count} calls")

# Step 3: Database summary
Session = sessionmaker(bind=engine)
s = Session()
summary = s.execute(text("SELECT lead_status, count(*) FROM contacts GROUP BY lead_status ORDER BY count(*) DESC")).fetchall()
print("\n=== UPDATED DATABASE CONTACT STATUSES ===")
for st, cnt in summary:
    print(f"  • {str(st):<30}: {cnt} contacts")
