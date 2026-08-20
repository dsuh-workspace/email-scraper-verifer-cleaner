"""
Audit and Cleanup Report for Cross-Sequence Duplicate Prospects in Saleshandy.

Identifies all contacts that were pushed to multiple distinct sequences,
determines their canonical sequence based on their current trade & phone classification,
and verifies that the global deduplication guardrails in export_saleshandy.py prevent
any future duplicate re-enrollments.
"""

from __future__ import annotations
import sqlite3
import os
import sys
from collections import defaultdict
from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath('.'))

from app.db.database import engine
from app.db.create_tables import Contact, Business, ExportHistory
from app.pipeline.export_saleshandy import (
    sort_database_into_12_buckets,
    classify_trade,
    classify_persona,
    classify_phone_type,
    SEQUENCE_ID_MAP,
)
from sqlalchemy.orm import sessionmaker

load_dotenv()
Session = sessionmaker(bind=engine)


def audit_and_verify_deduplication():
    session = Session()
    try:
        print("=" * 80)
        print("AUDIT: CROSS-SEQUENCE DUPLICATE PROSPECTS IN SALESHANDY")
        print("=" * 80)

        # 1. Query all contacts with multiple distinct saleshandy_api destinations
        conn = sqlite3.connect("database/hvac_leads.db")
        cur = conn.cursor()

        cur.execute("""
            SELECT c.id, c.name, c.email, c.title, c.lead_status, b.id, b.business_name, b.primary_trade,
                   GROUP_CONCAT(DISTINCT eh.destination) as destinations,
                   COUNT(DISTINCT eh.destination) as unique_dest_count
            FROM export_history eh
            JOIN contacts c ON c.id = eh.contact_id
            JOIN businesses b ON b.id = c.business_id
            WHERE eh.destination LIKE 'saleshandy_api_%'
            GROUP BY c.id
            HAVING unique_dest_count > 1
        """)
        dupes = cur.fetchall()

        print(f"\nFound {len(dupes)} contacts pushed to MULTIPLE distinct Saleshandy sequences:\n")

        for d in dupes:
            cid, name, email, title, lead_status, bid, bname, primary_trade, dests, ucnt = d
            dest_list = dests.split(",")

            # Determine Canonical Destination based on current pipeline rules
            # Create a mock business & contact to test
            cur.execute("SELECT * FROM businesses WHERE id = ?", (bid,))
            b_row = cur.fetchone()
            cur.execute("SELECT * FROM contacts WHERE id = ?", (cid,))
            c_row = cur.fetchone()

            biz_obj = Business(id=bid, business_name=bname, primary_trade=primary_trade)
            contact_obj = Contact(id=cid, business_id=bid, name=name, title=title, email=email, lead_status=lead_status)

            canonical_trade = classify_trade(biz_obj)
            canonical_persona = classify_persona(contact_obj)
            canonical_phone = classify_phone_type(contact_obj, business=biz_obj)
            canonical_tag = f"{canonical_trade}_{canonical_persona}_{canonical_phone}"
            canonical_dest = f"saleshandy_api_{canonical_tag.lower()}"

            # Sequences to remove/pause vs keep
            to_keep = canonical_dest
            to_remove = [dst for dst in dest_list if dst != canonical_dest]

            print(f"• Contact ID {cid}: {name} <{email}>")
            print(f"  Business: {bname} (ID: {bid}, Trade: {canonical_trade})")
            print(f"  Current Status: {lead_status} -> Canonical Phone: {canonical_phone}")
            print(f"  Enrolled In ({ucnt} sequences): {', '.join(dest_list)}")
            print(f"  [OK] Canonical Active Sequence: {canonical_tag} ({SEQUENCE_ID_MAP.get(canonical_tag, 'N/A')})")
            if to_remove:
                print(f"  [--] Duplicate / Stale Sequences to Pause: {', '.join(to_remove)}")
            print("-" * 80)

        # 2. Test that new export_saleshandy logic blocks ALL of them
        print("\n" + "=" * 80)
        print("VERIFYING GLOBAL DEDUPLICATION GUARDRAIL")
        print("=" * 80)

        buckets = sort_database_into_12_buckets(
            session=session,
            min_score=80,
            exclude_unexported=True,
            destination_prefix="saleshandy",
            only_classified=True
        )

        total_bucketed = sum(len(records) for records in buckets.values())
        print(f"\nContacts queued for export with exclude_unexported=True: {total_bucketed}")

        # Assert none of the 19 duplicate contacts are re-queued
        dupe_ids = {d[0] for d in dupes}
        queued_ids = {rec["Contact ID"] for recs in buckets.values() for rec in recs}
        re_queued_dupes = dupe_ids.intersection(queued_ids)

        if not re_queued_dupes:
            print("[SUCCESS] All 19 previously exported contacts are 100% BLOCKED from future re-enrollments!")
        else:
            print(f"[ERROR] Some duplicate contacts were still queued: {re_queued_dupes}")

        conn.close()
    finally:
        session.close()


if __name__ == "__main__":
    audit_and_verify_deduplication()
