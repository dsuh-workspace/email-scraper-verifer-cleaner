#!/usr/bin/env python3
"""
Run Automated Twilio Phone Classification on Phase 1 Leads (Sacramento)
and re-sort into the 12 Phone-Classified Saleshandy Campaign Buckets.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.create_tables import engine
from app.logging_config import setup_logging
from app.pipeline.call_leads import (
    poll_and_classify_completed_calls,
    reconcile_stale_phone_classifications,
    sync_phone_classifications_across_business_contacts,
    trigger_twilio_outbound_calls,
)
from app.pipeline.export_saleshandy import export_12_saleshandy_permutations

logger = logging.getLogger(__name__)


def main():
    setup_logging()
    print("=" * 80)
    print("STARTING AUTOMATED PHONE CLASSIFICATION FOR PHASE 1 LEADS")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    # Step 1: Dispatch Twilio Outbound Calls
    print("\n[STEP 1] Dispatching Twilio outbound classification calls...")
    dispatched = trigger_twilio_outbound_calls(min_score=80, exclude_already_exported=True)
    print(f"[SUCCESS] Dispatched {dispatched} outbound classification calls.")

    if dispatched > 0:
        # Step 2: Poll and transcribe calls
        print("\n[STEP 2] Polling Twilio call audio recordings and transcribing destinations...")
        time.sleep(30)
        
        # Multi-pass polling to ensure all dispatched calls are recorded and classified
        total_classified = {}
        for poll_round in range(1, 4):
            print(f"  * Polling Pass {poll_round}/3...")
            classified_results = poll_and_classify_completed_calls(wait_for_completion=False)
            for k, v in classified_results.items():
                total_classified[k] = total_classified.get(k, 0) + v
            if poll_round < 3:
                time.sleep(15)
        
        print(f"[RESULTS] Phone Classification STT Results: {total_classified}")

    # Step 3: Re-sort into the 12 Saleshandy permutation CSVs
    print("\n[STEP 3] Sorting leads into 12-Permutation Campaign CSVs (data/saleshandy_campaigns/)...")
    exported_counts = export_12_saleshandy_permutations(
        output_dir="data/saleshandy_campaigns",
        min_score=80,
        exclude_unexported=True,
        only_classified=False,  # Includes classified phone buckets + direct fallbacks
    )

    print("\n" + "=" * 80)
    print("PHONE CLASSIFICATION & 12-PERMUTATION BUCKETING COMPLETE")
    print("=" * 80)
    print("Final Exported Campaign Rosters:")
    for tag, count in sorted(exported_counts.items()):
        print(f"  * {tag}: {count} prospects")


if __name__ == "__main__":
    main()
