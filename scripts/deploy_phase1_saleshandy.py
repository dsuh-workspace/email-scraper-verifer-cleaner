#!/usr/bin/env python3
"""
Deploy Classified Phase 1 Leads (Sacramento) directly to Live Saleshandy Sequences via API.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.logging_config import setup_logging
from app.pipeline.export_saleshandy import push_to_saleshandy_api

logger = logging.getLogger(__name__)


def main():
    setup_logging()
    print("=" * 80)
    print("STARTING LIVE SALESHANDY CAMPAIGN DEPLOYMENT (SACRAMENTO PHASE 1)")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    results = push_to_saleshandy_api(
        min_score=80,
        exclude_unexported=True,
        only_classified=False,  # Enrolls classified phone buckets + direct email fallbacks
        destination_prefix="saleshandy_api",
    )

    print("\n" + "=" * 80)
    print("SALESHANDY DEPLOYMENT COMPLETE")
    print("=" * 80)
    total_pushed = sum(results.values())
    print(f"Total Leads Pushed to Saleshandy: {total_pushed}\n")
    print("Breakdown by Sequence:")
    for tag, count in sorted(results.items()):
        if count > 0:
            print(f"  * {tag}: {count} prospects deployed")


if __name__ == "__main__":
    main()
