import os
import sys
sys.path.insert(0, os.path.abspath('.'))

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import init_db, ScrapeRun, RawLead, Business, Contact, EmailVerification, ExportHistory
from app.pipeline.export_saleshandy import export_12_saleshandy_permutations

def main():
    run_id = 96
    Session = sessionmaker(bind=engine)
    session = Session()

    print("\n" + "=" * 75)
    print("       SANTA CLARITA HVAC PIPELINE EXECUTION REPORT (RUN 96)       ")
    print("=" * 75)

    total_raw = session.query(RawLead).filter(RawLead.scrape_run_id == run_id).count()
    net_new_biz = session.query(Business).filter(Business.first_scrape_run_id == run_id).count()
    cohort_contacts = session.query(Contact).filter(Contact.first_scrape_run_id == run_id).all()
    contacts_with_email = [c for c in cohort_contacts if c.email]

    # Email verification stats
    verif_query = (
        session.query(EmailVerification.status, func.count(EmailVerification.id))
        .join(Contact, Contact.id == EmailVerification.contact_id)
        .filter(Contact.first_scrape_run_id == run_id)
        .group_by(EmailVerification.status)
        .all()
    )
    verif_stats = dict(verif_query)

    # Phone classification stats
    status_query = (
        session.query(Contact.lead_status, func.count(Contact.id))
        .filter(Contact.first_scrape_run_id == run_id)
        .group_by(Contact.lead_status)
        .all()
    )
    status_stats = dict(status_query)

    session.close()

    # Generate 12-permutation CSVs
    print("\nExporting 12-Permutation Campaign Matrix to 'data/saleshandy_campaigns/'...")
    exported_counts = export_12_saleshandy_permutations(
        output_dir="data/saleshandy_campaigns",
        min_score=80,
        exclude_unexported=False,
        only_classified=True
    )

    print(f"\n[1] Ingestion & Provenance:")
    print(f"    • Source: Outscraper-20260818200950s730e.xlsx (Santa Clarita, CA)")
    print(f"    • Scrape Run ID: {run_id}")
    print(f"    • Total Raw Leads Ingested: {total_raw}")
    print(f"    • Net-New Businesses Added: {net_new_biz}")
    print(f"    • Total Cohort Contacts: {len(cohort_contacts)}")
    print(f"    • Contacts with Email Address: {len(contacts_with_email)}")

    print(f"\n[2] Email Deliverability Verification Breakdown:")
    for k, v in verif_stats.items():
        print(f"    • {k.capitalize()}: {v} emails")

    print(f"\n[3] Twilio Outbound Phone Classifier Breakdown:")
    for k, v in sorted(status_stats.items(), key=lambda x: str(x[0])):
        print(f"    • {k}: {v} contacts")

    print(f"\n[4] Segmented Saleshandy CSV Cohorts (Ready in data/saleshandy_campaigns/):")
    total_staged = 0
    for perm_tag, count in exported_counts.items():
        if count > 0:
            print(f"    • {perm_tag}.csv: {count} leads")
            total_staged += count
    print(f"    --------------------------------------------------")
    print(f"    Total Verified & Classified Leads Ready for Pitch: {total_staged}")
    print("=" * 75)

if __name__ == "__main__":
    main()
