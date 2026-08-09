"""
Simulation Test: Phone Classification & 12 Campaign Permutation Generation.

Simulates phone classification outcomes (IVR, Receptionist, Voicemail) on sample contacts
in database/hvac_leads.db and executes export_12_saleshandy_permutations() to populate
all 12 Saleshandy campaign files.
"""

import sys
from pathlib import Path
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.database import engine
from app.db.create_tables import Contact, Business
from app.pipeline.export_saleshandy import export_12_saleshandy_permutations

Session = sessionmaker(bind=engine)


def run_simulation():
    session = Session()
    try:
        print("=" * 70)
        print("SIMULATING PHONE CLASSIFICATIONS & RECREATING 12 CAMPAIGNS")
        print("=" * 70)

        contacts = session.query(Contact).filter(Contact.email.isnot(None), Contact.email != "").all()
        businesses = session.query(Business).all()

        if not contacts:
            print("No contacts found in database to simulate.")
            return

        print(f"Loaded {len(contacts)} contacts from database/hvac_leads.db.")

        # 1. Simulate phone classification statuses across contacts
        for i, c in enumerate(contacts):
            mod = i % 3
            if mod == 0:
                c.lead_status = "Classified_IVR"
            elif mod == 1:
                c.lead_status = "Classified_Receptionist"
            else:
                c.lead_status = "Classified_Voicemail"

        # 2. Assign half businesses to HVAC and half to Plumbing for complete trade distribution
        for i, b in enumerate(businesses):
            if i % 2 == 0:
                b.category = "HVAC contractor, Air conditioning repair service, Heating contractor"
            else:
                b.category = "Plumber, Drainage service, Water heater repair"

        session.commit()
        print("Successfully updated sample database records with simulated IVR, Receptionist, and Voicemail statuses.")
        print("-" * 70)

        # 3. Export 12 Saleshandy campaign files
        counts = export_12_saleshandy_permutations("data/saleshandy_campaigns")

        print("\nRecreated 12 Campaign Permutations Summary:")
        print("-" * 70)
        total_bucketed = 0
        for perm_tag, count in counts.items():
            total_bucketed += count
            status_indicator = "[POPULATED]" if count > 0 else "[EMPTY]"
            print(f"  {status_indicator:12s} {perm_tag:35s}: {count:3d} leads")

        print("-" * 70)
        print(f"Total Contacts Bucketed across 12 Campaign Permutations: {total_bucketed}")
        print("=" * 70)

    finally:
        session.close()


if __name__ == "__main__":
    run_simulation()
