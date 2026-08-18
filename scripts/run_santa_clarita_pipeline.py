import os
import sys
sys.path.insert(0, os.path.abspath('.'))

import time
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from app.db.database import engine
from sqlalchemy.orm import sessionmaker
from app.db.create_tables import init_db, ScrapeRun, RawLead, Business, Contact, EmailVerification, ExportHistory
from app.pipeline.process_leads import process_and_deduplicate_leads
from app.pipeline.tomba_enricher import enrich_businesses_with_tomba
from app.pipeline.verify_emails import verify_contacts_emails
from app.pipeline.call_leads import trigger_twilio_outbound_calls, poll_and_classify_completed_calls
from app.pipeline.export_saleshandy import export_12_saleshandy_permutations

def parse_outscraper_xlsx(file_path: str):
    """Parse Outscraper XLSX file into list of row dicts."""
    records = []
    with zipfile.ZipFile(file_path, 'r') as z:
        shared_strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            tree = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for elem in tree.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si'):
                text = "".join(t.text for t in elem.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t') if t.text)
                shared_strings.append(text)
        
        sheet_tree = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        rows = sheet_tree.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row')
        if not rows:
            return records
        
        headers = []
        for cell in rows[0].findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'):
            t = cell.attrib.get('t')
            v = cell.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
            val = v.text if v is not None else ''
            if t == 's' and val.isdigit() and int(val) < len(shared_strings):
                val = shared_strings[int(val)]
            headers.append(val)
            
        for row in rows[1:]:
            row_vals = {}
            cells = row.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c')
            for cell in cells:
                r_ref = cell.attrib.get('r')
                col_idx = 0
                col_letters = ''.join(c for c in r_ref if c.isalpha())
                for char in col_letters:
                    col_idx = col_idx * 26 + (ord(char.upper()) - ord('A') + 1)
                col_idx -= 1
                
                t = cell.attrib.get('t')
                v = cell.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                val = v.text if v is not None else ''
                if t == 's' and val.isdigit() and int(val) < len(shared_strings):
                    val = shared_strings[int(val)]
                
                if col_idx < len(headers):
                    row_vals[headers[col_idx]] = val
            records.append(row_vals)
    return records

def main():
    print("=" * 70, flush=True)
    print("STARTING SANTA CLARITA HVAC OUTSCRAPER PIPELINE RUN", flush=True)
    print("=" * 70, flush=True)
    
    init_db()
    file_path = os.path.expanduser(r'~\Downloads\Outscraper-20260818200950s730e.xlsx')
    if not os.path.exists(file_path):
        file_path = r'C:\Users\Daniel\Downloads\Outscraper-20260818200950s730e.xlsx'
    
    print(f"Reading Outscraper file: {file_path}", flush=True)
    records = parse_outscraper_xlsx(file_path)
    print(f"Parsed {len(records)} records from XLSX.", flush=True)

    Session = sessionmaker(bind=engine)
    session = Session()

    # Step 1: Create ScrapeRun for Outscraper Ingest
    now = datetime.now(timezone.utc)
    scrape_run = ScrapeRun(
        query="Outscraper Ingest - Santa Clarita HVAC",
        location="Santa Clarita, CA",
        category="HVAC",
        status="completed",
        started_at=now,
        completed_at=now
    )
    session.add(scrape_run)
    session.flush()
    run_id = scrape_run.id
    print(f"\n[PHASE 1] Created ScrapeRun ID: {run_id}", flush=True)

    initial_biz_count = session.query(Business).count()
    initial_contact_count = session.query(Contact).count()

    raw_count = 0
    for r in records:
        b_name = r.get('name') or r.get('name_for_emails')
        if not b_name:
            continue
        
        rc = None
        if r.get('reviews') and r.get('reviews').isdigit():
            rc = int(r.get('reviews'))
        rr = None
        if r.get('rating'):
            try:
                rr = float(r.get('rating'))
            except ValueError:
                pass

        raw_lead = RawLead(
            scrape_run_id=run_id,
            business_name=b_name,
            category=r.get('category') or r.get('type') or r.get('subtypes'),
            phone=r.get('phone'),
            website=r.get('website'),
            email=r.get('email') or r.get('emails'),
            review_count=rc,
            review_rating=rr,
            address=r.get('address') or r.get('full_address'),
            place_id=r.get('place_id') or r.get('google_id'),
            processed_at=None
        )
        session.add(raw_lead)
        raw_count += 1

    session.commit()
    print(f"Inserted {raw_count} raw leads into database for run {run_id}.", flush=True)
    session.close()

    # Step 2: Lead Processing & Deduplication
    print("\n[PHASE 2] Running Lead Deduplication & Ingestion...", flush=True)
    process_and_deduplicate_leads()

    session = Session()
    post_dedupe_biz_count = session.query(Business).count()
    post_dedupe_contact_count = session.query(Contact).count()
    new_biz_count = post_dedupe_biz_count - initial_biz_count
    new_contact_count = post_dedupe_contact_count - initial_contact_count
    print(f"Deduplication complete: {new_biz_count} net-new businesses, {new_contact_count} net-new contacts.", flush=True)
    session.close()

    # Step 3: Tomba Decision-Maker Enrichment
    print("\n[PHASE 3] Running Tomba Decision-Maker Enrichment...", flush=True)
    try:
        tomba_added = enrich_businesses_with_tomba(fallback_only=False)
        print(f"Tomba enrichment complete: Added {tomba_added} decision-maker contacts.", flush=True)
    except Exception as e:
        print(f"Tomba enrichment note: {e}", flush=True)
        tomba_added = 0

    # Step 4: Email Deliverability Verification
    print("\n[PHASE 4] Running Email Deliverability Verification...", flush=True)
    try:
        verify_contacts_emails()
        print("Email verification complete.", flush=True)
    except Exception as e:
        print(f"Email verification note: {e}", flush=True)

    # Step 5: Twilio Phone Classification
    print("\n[PHASE 5] Running Twilio Phone Classification...", flush=True)
    try:
        calls_dispatched = trigger_twilio_outbound_calls(min_score=80)
        print(f"Dispatched {calls_dispatched} outbound classification calls.", flush=True)
        if calls_dispatched > 0:
            print("Polling call recordings and running STT classification...", flush=True)
            classified_results = poll_and_classify_completed_calls(wait_for_completion=True, max_wait_sec=60)
            print(f"Phone classification results: {classified_results}", flush=True)
        else:
            print("No new unclassified phone numbers to call.", flush=True)
    except Exception as e:
        print(f"Phone classification note: {e}", flush=True)

    # Step 6: 12-Permutation CSV Export
    print("\n[PHASE 6] Generating 12-Permutation Campaign CSVs (data/saleshandy_campaigns/)...", flush=True)
    exported_counts = export_12_saleshandy_permutations(
        output_dir="data/saleshandy_campaigns",
        min_score=80,
        exclude_unexported=False,
        only_classified=True
    )

    # Step 7: Final Comprehensive Metrics
    Session = sessionmaker(bind=engine)
    session = Session()
    
    total_raw = session.query(RawLead).filter(RawLead.scrape_run_id == run_id).count()
    net_new_biz = session.query(Business).filter(Business.first_scrape_run_id == run_id).count()
    cohort_contacts = session.query(Contact).filter(Contact.first_scrape_run_id == run_id).all()
    contacts_with_email = [c for c in cohort_contacts if c.email]

    # Email verification stats
    verif_query = (
        session.query(EmailVerification.status, sqlite3.func.count(EmailVerification.id))
        .join(Contact, Contact.id == EmailVerification.contact_id)
        .filter(Contact.first_scrape_run_id == run_id)
        .group_by(EmailVerification.status)
        .all()
    )
    verif_stats = dict(verif_query)

    # Phone classification stats
    status_query = (
        session.query(Contact.lead_status, sqlite3.func.count(Contact.id))
        .filter(Contact.first_scrape_run_id == run_id)
        .group_by(Contact.lead_status)
        .all()
    )
    status_stats = dict(status_query)

    session.close()

    print("\n" + "=" * 70, flush=True)
    print("      SANTA CLARITA HVAC PIPELINE COMPLETE — FINAL REPORT      ", flush=True)
    print("=" * 70, flush=True)
    print(f"Source File: Outscraper-20260818200950s730e.xlsx", flush=True)
    print(f"Scrape Run ID: {run_id}", flush=True)
    print(f"Total Raw Records Ingested: {total_raw}", flush=True)
    print(f"Net-New Businesses Created: {net_new_biz}", flush=True)
    print(f"Total Contacts for Cohort: {len(cohort_contacts)}", flush=True)
    print(f"Contacts with Email Address: {len(contacts_with_email)}", flush=True)
    print(f"Decision-Maker Emails Added via Tomba: {tomba_added}", flush=True)
    
    print("\nEmail Verification Breakdown:", flush=True)
    for k, v in verif_stats.items():
        print(f"  • {k}: {v} emails", flush=True)
        
    print("\nPhone Classification Breakdown:", flush=True)
    for k, v in status_stats.items():
        print(f"  • {k}: {v} contacts", flush=True)
        
    print("\nGenerated Saleshandy Campaign Matrix (data/saleshandy_campaigns/):", flush=True)
    for perm_tag, count in exported_counts.items():
        if count > 0:
            print(f"  • {perm_tag}: {count} leads", flush=True)
            
    print("=" * 70, flush=True)

if __name__ == "__main__":
    main()
