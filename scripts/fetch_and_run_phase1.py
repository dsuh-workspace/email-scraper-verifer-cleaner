#!/usr/bin/env python3
"""
Fetch Phase 1 Leads (Greater Sacramento and Capital Region) via Outscraper API
and run the 7-stage enrichment pipeline stopping at Stage 6 (before live Saleshandy push).
"""

import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import csv
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from scripts.run_outscraper_pipeline import run_outscraper_pipeline


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()

OUTSCRAPER_API_KEY = os.getenv("OUTSCRAPER_API_KEY")
OUTSCRAPER_SEARCH_URL = "https://api.app.outscraper.com/maps/search-v3"

PHASE_1_QUERIES = [
    # Sacramento County
    "HVAC contractor in Sacramento CA",
    "Plumber in Sacramento CA",
    "HVAC contractor in Elk Grove CA",
    "Plumber in Elk Grove CA",
    "HVAC contractor in Folsom CA",
    "Plumber in Folsom CA",
    "HVAC contractor in Citrus Heights CA",
    "Plumber in Citrus Heights CA",
    "HVAC contractor in Rancho Cordova CA",
    "Plumber in Rancho Cordova CA",
    # Placer County
    "HVAC contractor in Roseville CA",
    "Plumber in Roseville CA",
    "HVAC contractor in Rocklin CA",
    "Plumber in Rocklin CA",
    # Yolo County
    "HVAC contractor in Davis CA",
    "Plumber in Davis CA",
]


def fetch_outscraper_queries(queries: List[str], limit_per_query: int = 50) -> List[Dict[str, Any]]:
    """Fetch Google Maps listings for queries via Outscraper API."""
    if not OUTSCRAPER_API_KEY:
        raise ValueError("OUTSCRAPER_API_KEY not found in .env file.")

    headers = {"X-API-KEY": OUTSCRAPER_API_KEY}
    all_records: List[Dict[str, Any]] = []

    chunk_size = 4
    total_chunks = (len(queries) + chunk_size - 1) // chunk_size
    for i in range(0, len(queries), chunk_size):
        chunk = queries[i : i + chunk_size]
        chunk_num = (i // chunk_size) + 1
        logger.info(f"Fetching Outscraper chunk {chunk_num}/{total_chunks}: {chunk}")

        params = {
            "query": chunk,
            "limit": limit_per_query,
            "language": "en",
            "region": "US",
            "async": "false",
        }

        try:
            resp = requests.get(OUTSCRAPER_SEARCH_URL, headers=headers, params=params, timeout=120)
            if resp.status_code != 200:
                logger.error(f"Outscraper API HTTP {resp.status_code}: {resp.text[:300]}")
                continue

            data = resp.json()
            results_buckets = data.get("data", [])

            for query_idx, bucket in enumerate(results_buckets):
                q_name = chunk[query_idx] if query_idx < len(chunk) else "Unknown"
                logger.info(f"  -> '{q_name}': received {len(bucket)} listings")
                for item in bucket:
                    record = dict(item)
                    all_records.append(record)

        except Exception as e:
            logger.error(f"Error fetching chunk {chunk}: {e}")

        time.sleep(1.0)

    return all_records


def save_records_to_csv(records: List[Dict[str, Any]], output_path: str) -> str:
    """Save parsed records into a clean CSV file."""
    if not records:
        raise ValueError("No records to save.")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    fieldnames_set = set()
    for r in records:
        fieldnames_set.update(r.keys())

    preferred_order = [
        "query", "name", "name_for_emails", "type", "category", "subtypes",
        "phone", "website", "address", "full_address", "city", "state", "postal_code",
        "rating", "reviews", "place_id", "google_id", "email", "emails",
    ]
    remaining = sorted(fieldnames_set - set(preferred_order))
    fieldnames = [f for f in preferred_order if f in fieldnames_set] + remaining

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({k: (v if v is not None else "") for k, v in r.items()})

    logger.info(f"Saved {len(records)} records to {output_path}")
    return output_path


def main():
    print("=" * 80)
    print("STARTING OUTSCRAPER PHASE 1 SCRAPE (GREATER SACRAMENTO & CAPITAL REGION)")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    logger.info(f"Targeting {len(PHASE_1_QUERIES)} queries across Sacramento, Roseville, Elk Grove, Folsom, Rocklin, Davis...")
    records = fetch_outscraper_queries(PHASE_1_QUERIES, limit_per_query=50)
    print(f"\n[SUCCESS] Outscraper API returned a total of {len(records)} listings.")

    if not records:
        print("[ERROR] No records were retrieved from Outscraper. Exiting.")
        sys.exit(1)

    csv_file = "data/Outscraper_Sacramento_Phase1.csv"
    save_records_to_csv(records, csv_file)

    print("\n" + "=" * 80)
    print("INGESTING INTO PIPELINE (STAGES 1 TO 6 - STOPPING BEFORE SALESHANDY PUSH)")
    print("=" * 80)

    results = run_outscraper_pipeline(
        file_path=csv_file,
        skip_tomba=False,
        skip_verify=False,
        skip_calls=False,
        skip_saleshandy_push=True,
        min_score=80,
    )

    print("\n[PIPELINE COMPLETE - STOPPED AT STAGE 6 AS REQUESTED]")
    print(f"Exported lead counts by bucket: {results}")


if __name__ == "__main__":
    main()
