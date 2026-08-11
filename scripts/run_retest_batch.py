"""
Runner script for 25-call retest batch to compare AMD classification performance before and after parameter tuning.
"""

import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import Contact, Business
from app.pipeline.call_leads import trigger_twilio_outbound_calls
from scripts.check_twilio_calls import check_twilio_calls

load_dotenv()
Session = sessionmaker(bind=engine)


def run_retest():
    session = Session()
    try:
        print("=" * 70)
        print("STARTING 25-CALL LIVE RETEST BATCH (NEW AMD PARAMETERS)")
        print("=" * 70)

        # Query 25 distinct businesses with valid phone numbers currently in Classified_Voicemail or Unanswered_Retry
        target_businesses = (
            session.query(Business)
            .join(Contact, Contact.business_id == Business.id)
            .filter(Business.phone.isnot(None), Business.phone != "")
            .filter(Contact.lead_status.in_(("Classified_Voicemail", "Unanswered_Retry")))
            .distinct()
            .limit(25)
            .all()
        )

        if not target_businesses:
            print("No matching businesses found for retest.")
            return

        biz_ids = [b.id for b in target_businesses]
        print("Selected 25 distinct businesses for retest.", flush=True)

        # Record baseline status before update
        baseline_contacts = session.query(Contact).filter(Contact.business_id.in_(biz_ids)).all()
        before_counts = {}
        for c in baseline_contacts:
            before_counts[c.lead_status] = before_counts.get(c.lead_status, 0) + 1

        print("\nBASELINE STATUS (BEFORE RETEST):", flush=True)
        for st, cnt in before_counts.items():
            print(f"  {st:25s}: {cnt}", flush=True)

        # Reset target contacts at these businesses to 'Unanswered_Retry' so trigger_twilio_outbound_calls picks them up
        for c in baseline_contacts:
            c.lead_status = "Unanswered_Retry"
        session.commit()

        print("\nDispatching 25 outbound calls via Twilio with tuned AMD parameters...", flush=True)
        print("Parameters: MachineDetection=DetectMessageEnd, SpeechThreshold=4500ms, MachineWordsThreshold=12, Timeout=10s", flush=True)
        dispatched = trigger_twilio_outbound_calls(limit=25)
        print(f"\nSuccessfully dispatched {dispatched} live outbound calls.", flush=True)

        print("\nWaiting for calls to process and polling Twilio results...", flush=True)
        # Poll for 60 seconds (calls take ~5-15s to complete and report AMD result)
        for t in range(12):
            time.sleep(5)
            print(f"  Polling progress ({ (t+1)*5 }s)...", flush=True)
            check_twilio_calls()

        # Fetch AFTER status breakdown
        after_contacts = session.query(Contact).filter(Contact.business_id.in_(biz_ids)).all()
        after_counts = {}
        for c in after_contacts:
            after_counts[c.lead_status] = after_counts.get(c.lead_status, 0) + 1

        print("\n" + "=" * 70, flush=True)
        print("RETEST RESULTS SUMMARY (BEFORE vs AFTER)", flush=True)
        print("=" * 70, flush=True)
        print(f"{'Status':30s} | {'BEFORE':10s} | {'AFTER':10s}", flush=True)
        print("-" * 60, flush=True)
        all_statuses = set(before_counts.keys()).union(set(after_counts.keys()))
        for st in sorted(all_statuses):
            b_cnt = before_counts.get(st, 0)
            a_cnt = after_counts.get(st, 0)
            print(f"{st:30s} | {b_cnt:10d} | {a_cnt:10d}", flush=True)
        print("=" * 70, flush=True)

    finally:
        session.close()


if __name__ == "__main__":
    run_retest()
