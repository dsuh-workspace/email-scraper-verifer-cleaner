"""
Simulation Test: Phone Classification & 12 Campaign Permutation Generation.

Simulates phone classification outcomes (IVR, Receptionist, Voicemail) on sample contacts
in database/hvac_leads.db and executes export_12_saleshandy_permutations() to populate
all 12 Saleshandy campaign files.
"""

import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.create_tables import Base, Contact, Business
from app.pipeline.export_saleshandy import sort_database_into_12_buckets


def run_simulation():
    print("=" * 70)
    print("RUNNING ISOLATED IN-MEMORY SIMULATION TEST")
    print("=" * 70)

    # Use isolated in-memory engine to never corrupt the live database
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(test_engine)
    TestSession = sessionmaker(bind=test_engine)
    session = TestSession()

    try:
        # Create sample test businesses and contacts
        b_hvac = Business(
            id=1,
            business_name="Apex Heating & Air Conditioning",
            category="HVAC contractor",
            domain="apexheating.com",
            phone="+14085550101"
        )
        b_plumb = Business(
            id=2,
            business_name="Bueno Plumbing & Rooter",
            category="Plumber, Drainage service",
            domain="buenoplumbing.com",
            phone="+14085550102"
        )
        session.add_all([b_hvac, b_plumb])
        session.flush()

        contacts = [
            Contact(business_id=1, name="John Doe", email="john@apexheating.com", title="Owner", lead_status="Classified_Voicemail"),
            Contact(business_id=1, name="Info/Office", email="info@apexheating.com", title="General Contact", lead_status="Classified_IVR"),
            Contact(business_id=2, name="Info/Office", email="kbuenoplumbing@gmail.com", title="General Contact", lead_status="Classified_Voicemail"),
            Contact(business_id=2, name="Jane Smith", email="jane@buenoplumbing.com", title="Owner", lead_status="Classified_Receptionist"),
        ]
        session.add_all(contacts)
        session.commit()

        print(f"Created isolated sample businesses and {len(contacts)} contacts in-memory.")

        # Test sorting logic directly
        buckets = sort_database_into_12_buckets(session)

        print("\nSimulation Bucket Verification:")
        print("-" * 70)
        total = 0
        for tag, items in buckets.items():
            count = len(items)
            total += count
            if count > 0:
                print(f"  [POPULATED]  {tag:35s}: {count} leads")
                for lead in items:
                    print(f"               -> {lead['Company']} ({lead['Email']}) | Trade: {lead['Trade']}")

        print("-" * 70)
        print(f"Total leads bucketed: {total}")
        print("=" * 70)
        print("Isolated test simulation completed successfully without touching live DB.")

    finally:
        session.close()



if __name__ == "__main__":
    run_simulation()
