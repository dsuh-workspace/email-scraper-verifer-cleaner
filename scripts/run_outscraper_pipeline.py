"""
Universal Outscraper End-to-End Pipeline Runner.

Runs the complete 7-Stage Lead Generation & Deployment Pipeline on any Outscraper XLSX/CSV export:
  Stage 1: Raw Ingestion & Provenance Tracking (ScrapeRun + RawLead)
  Stage 2: Lead Deduplication & Junk Filtering (Domain -> Name + Phone)
  Stage 3: Decision-Maker Enrichment via Tomba API
  Stage 4: Email Deliverability Verification via Local Engine (Reacher)
  Stage 5: Automated Phone Classification via Twilio + Speech-to-Text
  Stage 6: 12-Permutation Campaign Sorting with Global Deduplication
  Stage 7: Live Saleshandy API Deployment into Target Sequences

Usage:
  python scripts/run_outscraper_pipeline.py "C:\\Users\\Daniel\\Downloads\\Outscraper-20260819222442s9543.xlsx"
  python scripts/run_outscraper_pipeline.py --file "path/to/outscraper.xlsx" --skip-phone-calls
"""

from __future__ import annotations
import argparse
import csv
import logging
import os
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath("."))

from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from app.db.create_tables import (
    init_db,
    ScrapeRun,
    RawLead,
    Business,
    Contact,
    EmailVerification,
    ExportHistory,
)
from app.db.database import engine
from app.logging_config import setup_logging
from app.pipeline.call_leads import trigger_twilio_outbound_calls, poll_and_classify_completed_calls
from app.pipeline.export_saleshandy import (
    export_12_saleshandy_permutations,
    push_to_saleshandy_api,
    SEQUENCE_ID_MAP,
)
from app.pipeline.process_leads import process_and_deduplicate_leads
from app.pipeline.tomba_enricher import enrich_businesses_with_tomba
from app.pipeline.verify_emails import verify_contacts_emails

load_dotenv()
logger = logging.getLogger(__name__)
Session = sessionmaker(bind=engine)


def parse_outscraper_file(file_path: str) -> list[dict[str, str]]:
    """Parse Outscraper XLSX or CSV file into a list of row dictionaries."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Outscraper file not found: {file_path}")

    records: list[dict[str, str]] = []

    if path.suffix.lower() == ".csv":
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append({k.strip(): (v or "").strip() for k, v in row.items() if k})
        return records

    # Parse .xlsx using stdlib zipfile + xml to avoid heavy dependencies
    with zipfile.ZipFile(path, "r") as z:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for elem in tree.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                text = "".join(
                    t.text
                    for t in elem.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
                    if t.text
                )
                shared_strings.append(text)

        sheet_tree = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows = sheet_tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row")
        if not rows:
            return records

        headers: list[str] = []
        for cell in rows[0].findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
            t = cell.attrib.get("t")
            v = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
            val = v.text if v is not None else ""
            if t == "s" and val.isdigit() and int(val) < len(shared_strings):
                val = shared_strings[int(val)]
            headers.append(val.strip())

        for row in rows[1:]:
            row_vals: dict[str, str] = {}
            cells = row.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")
            for cell in cells:
                r_ref = cell.attrib.get("r")
                col_letters = "".join(c for c in r_ref if c.isalpha())
                col_idx = 0
                for char in col_letters:
                    col_idx = col_idx * 26 + (ord(char.upper()) - ord("A") + 1)
                col_idx -= 1

                t = cell.attrib.get("t")
                v = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                val = v.text if v is not None else ""
                if t == "s" and val.isdigit() and int(val) < len(shared_strings):
                    val = shared_strings[int(val)]

                if col_idx < len(headers):
                    row_vals[headers[col_idx]] = val.strip()

            if any(row_vals.values()):
                records.append(row_vals)

    return records


def run_outscraper_pipeline(
    file_path: str,
    skip_tomba: bool = False,
    skip_verify: bool = False,
    skip_calls: bool = False,
    skip_saleshandy_push: bool = False,
    min_score: int = 80,
) -> dict[str, int]:
    """Execute the full 7-stage Outscraper lead pipeline."""
    setup_logging()
    init_db()

    print("=" * 80, flush=True)
    print("STARTING COMPLETE OUTSCRAPER END-TO-END LEAD PIPELINE", flush=True)
    print(f"Target File: {file_path}", flush=True)
    print(f"Timestamp:   {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("=" * 80, flush=True)

    records = parse_outscraper_file(file_path)
    print(f"\n[INFO] Successfully parsed {len(records)} records from {file_path}.", flush=True)
    if not records:
        print("[WARNING] No records found in file. Exiting pipeline.", flush=True)
        return {}

    session = Session()

    # --- STAGE 1: ScrapeRun & RawLead Ingest ---
    print("\n" + "-" * 80, flush=True)
    print("STAGE 1: Provenance Tracking & Raw Lead Ingest", flush=True)
    print("-" * 80, flush=True)

    # Infer location / query from records
    sample_city = next((r.get("city") for r in records if r.get("city")), "Santa Clara")
    sample_state = next((r.get("state_code") or r.get("state") for r in records if r.get("state_code") or r.get("state")), "CA")
    loc_label = f"{sample_city}, {sample_state}"
    file_basename = Path(file_path).name

    now = datetime.now(timezone.utc)
    scrape_run = ScrapeRun(
        query=f"Outscraper Ingest - {file_basename}",
        location=loc_label,
        category="HVAC/Plumbing",
        status="completed",
        started_at=now,
        completed_at=now,
    )
    session.add(scrape_run)
    session.flush()
    run_id = scrape_run.id
    print(f"* Created ScrapeRun ID #{run_id} ('{scrape_run.query}')", flush=True)

    initial_biz_count = session.query(Business).count()
    initial_contact_count = session.query(Contact).count()

    raw_count = 0
    skipped_non_contractor = 0
    for r in records:
        b_name = r.get("name") or r.get("name_for_emails") or r.get("company_name")
        if not b_name:
            continue

        cat_field = (r.get("category") or r.get("type") or r.get("subtypes") or "").strip()
        cat_lower = cat_field.lower()
        if cat_lower and any(k in cat_lower for k in ("trade school", "postal code", "supply store", "pipe supplier", "union local")):
            if not any(k in cat_lower for k in ("contractor", "repair", "plumber", "heating", "cooling", "drain")):
                skipped_non_contractor += 1
                continue

        rc = None
        reviews_str = r.get("reviews") or r.get("reviews_count")
        if reviews_str and reviews_str.isdigit():
            rc = int(reviews_str)

        rr = None
        rating_str = r.get("rating")
        if rating_str:
            try:
                rr = float(rating_str)
            except ValueError:
                pass

        raw_lead = RawLead(
            scrape_run_id=run_id,
            business_name=b_name,
            category=cat_field,
            phone=r.get("phone") or r.get("company_phone"),
            website=r.get("website"),
            email=r.get("email") or r.get("emails"),
            review_count=rc,
            review_rating=rr,
            address=r.get("address") or r.get("full_address"),
            place_id=r.get("place_id") or r.get("google_id"),
            processed_at=None,
        )
        session.add(raw_lead)
        raw_count += 1

    session.commit()
    print(f"* Ingested {raw_count} contractor leads into database linked to Run ID #{run_id} (Filtered out {skipped_non_contractor} non-contractor supply/school listings).", flush=True)

    # --- STAGE 2: Deduplication & Junk Filtering ---
    print("\n" + "-" * 80, flush=True)
    print("STAGE 2: Deduplication & Ingestion into Businesses & Contacts", flush=True)
    print("-" * 80, flush=True)
    process_and_deduplicate_leads()

    post_biz_count = session.query(Business).count()
    post_contact_count = session.query(Contact).count()
    new_biz = post_biz_count - initial_biz_count
    new_contacts = post_contact_count - initial_contact_count
    print(f"* Ingestion Complete: {new_biz} net-new businesses, {new_contacts} net-new contacts added.", flush=True)

    # --- STAGE 3: Decision-Maker Enrichment via Tomba ---
    tomba_added = 0
    if not skip_tomba:
        print("\n" + "-" * 80, flush=True)
        print("STAGE 3: Decision-Maker Enrichment (Tomba Domain Search API - Gate 1 & 2 Deduplication)", flush=True)
        print("-" * 80, flush=True)
        try:
            tomba_added = enrich_businesses_with_tomba(
                fallback_only=False,
                scrape_run_id=run_id,
                exclude_already_exported=True,
            )
            print(f"* Tomba Enrichment Complete: Added {tomba_added} verified decision-maker emails (Gate 2 deduplicated).", flush=True)
        except Exception as e:
            print(f"* Tomba Enrichment note: {e}", flush=True)
    else:
        print("\n[INFO] Skipping Stage 3 (Tomba enrichment) via flag.", flush=True)

    # --- STAGE 4: Email Deliverability Verification ---
    if not skip_verify:
        print("\n" + "-" * 80, flush=True)
        print("STAGE 4: Email Deliverability Verification (Local Reacher Engine)", flush=True)
        print("-" * 80, flush=True)
        try:
            verify_contacts_emails()
            print("* Email Verification Complete.", flush=True)
        except Exception as e:
            print(f"* Email Verification note: {e}", flush=True)
    else:
        print("\n[INFO] Skipping Stage 4 (Email Verification) via flag.", flush=True)

    # --- STAGE 5: Automated Twilio Phone Classification ---
    if not skip_calls:
        print("\n" + "-" * 80, flush=True)
        print("STAGE 5: Automated Phone Classification (Twilio - Net-New Leads Only)", flush=True)
        print("-" * 80, flush=True)
        try:
            calls_dispatched = trigger_twilio_outbound_calls(
                min_score=min_score,
                exclude_already_exported=True,
            )
            print(f"* Dispatched {calls_dispatched} outbound classification calls to net-new businesses.", flush=True)
            if calls_dispatched > 0:
                print("* Polling call audio recordings and transcribing destinations...", flush=True)
                classified_results = poll_and_classify_completed_calls(wait_for_completion=True, max_wait_sec=90)
                print(f"* Phone Classification Results: {classified_results}", flush=True)
            else:
                print("* All leads already classified or no pending numbers.", flush=True)
        except Exception as e:
            print(f"* Phone Classification note: {e}", flush=True)
    else:
        print("\n[INFO] Skipping Stage 5 (Twilio Phone Calls) via flag.", flush=True)

    # --- STAGE 6: 12-Permutation Campaign Sorting & CSV Export ---
    print("\n" + "-" * 80, flush=True)
    print("STAGE 6: 12-Permutation Campaign Sorting with Global Deduplication", flush=True)
    print("-" * 80, flush=True)
    exported_counts = export_12_saleshandy_permutations(
        output_dir="data/saleshandy_campaigns",
        min_score=min_score,
        exclude_unexported=True,  # Enforce global deduplication
        only_classified=True,
    )
    for tag, cnt in exported_counts.items():
        if cnt > 0:
            print(f"  * {tag:<35} : {cnt:>3} leads -> data/saleshandy_campaigns/saleshandy_{tag.lower()}.csv", flush=True)

    # --- STAGE 7: Live Saleshandy API Deployment ---
    api_results = {}
    if not skip_saleshandy_push and os.getenv("SALESHANDY_API_KEY"):
        print("\n" + "-" * 80, flush=True)
        print("STAGE 7: Deploying Directly into Live Saleshandy Sequences", flush=True)
        print("-" * 80, flush=True)
        try:
            api_results = push_to_saleshandy_api(
                min_score=min_score,
                exclude_unexported=True,  # Enforce global deduplication
                only_classified=False,
            )
            total_pushed = sum(api_results.values())
            print(f"* Live API Deployment Complete: {total_pushed} leads enrolled into Saleshandy sequences.", flush=True)
        except Exception as e:
            print(f"* Saleshandy API Deployment note: {e}", flush=True)
    else:
        print("\n[INFO] Skipping Stage 7 (Live Saleshandy API Push).", flush=True)

    # --- SUMMARY REPORT ---
    print("\n" + "=" * 80, flush=True)
    print("                 PIPELINE EXECUTION SUMMARY REPORT                 ", flush=True)
    print("=" * 80, flush=True)
    print(f"Source File:                {file_basename}")
    print(f"Scrape Run ID:              #{run_id}")
    print(f"Raw Leads Ingested:         {raw_count}")
    print(f"Net-New Businesses:         {new_biz}")
    print(f"Net-New Contacts:           {new_contacts}")
    print(f"Tomba Decision Makers Added: {tomba_added}")

    # Query verification status for this cohort
    verif_counts = (
        session.query(EmailVerification.status, func.count(EmailVerification.id))
        .join(Contact, Contact.id == EmailVerification.contact_id)
        .filter(Contact.first_scrape_run_id == run_id)
        .group_by(EmailVerification.status)
        .all()
    )
    if verif_counts:
        print("\nEmail Deliverability Breakdown:")
        for st, c in verif_counts:
            print(f"  * {st}: {c}")

    # Query phone status for this cohort
    status_counts = (
        session.query(Contact.lead_status, func.count(Contact.id))
        .filter(Contact.first_scrape_run_id == run_id)
        .group_by(Contact.lead_status)
        .all()
    )
    if status_counts:
        print("\nPhone Classification Breakdown:")
        for st, c in status_counts:
            print(f"  * {st}: {c}")

    print("=" * 80, flush=True)
    session.close()
    return exported_counts


def main():
    parser = argparse.ArgumentParser(description="Run complete 7-stage Outscraper lead pipeline.")
    parser.add_argument("file", nargs="?", help="Path to Outscraper XLSX or CSV export file.")
    parser.add_argument("--file", dest="file_opt", help="Alternative flag for Outscraper file path.")
    parser.add_argument("--skip-tomba", action="store_true", help="Skip Tomba decision-maker enrichment.")
    parser.add_argument("--skip-verify", action="store_true", help="Skip Reacher email verification.")
    parser.add_argument("--skip-calls", action="store_true", help="Skip Twilio outbound phone classification.")
    parser.add_argument("--skip-saleshandy-push", action="store_true", help="Skip live API push to Saleshandy sequences.")
    parser.add_argument("--min-score", type=int, default=80, help="Minimum email verification score (default: 80).")

    args = parser.parse_args()
    target_file = args.file or args.file_opt

    if not target_file:
        # Check Downloads folder for most recent Outscraper file
        downloads = Path(os.path.expanduser("~/Downloads"))
        outscraper_files = sorted(downloads.glob("Outscraper-*.xlsx"), key=os.path.getmtime, reverse=True)
        if outscraper_files:
            target_file = str(outscraper_files[0])
            print(f"[AUTO-DETECT] Found latest Outscraper file in Downloads: {target_file}")
        else:
            parser.error("No Outscraper file specified and none auto-detected in ~/Downloads.")

    run_outscraper_pipeline(
        file_path=target_file,
        skip_tomba=args.skip_tomba,
        skip_verify=args.skip_verify,
        skip_calls=args.skip_calls,
        skip_saleshandy_push=args.skip_saleshandy_push,
        min_score=args.min_score,
    )


if __name__ == "__main__":
    main()
