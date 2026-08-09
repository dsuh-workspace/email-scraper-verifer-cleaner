"""
Unit tests for app/pipeline/export_saleshandy.py.

Tests every individual step of the 12-permutation sorting pipeline:
  - Trade classification (HVAC vs Plumbing)
  - Persona classification (Owner vs NonOwner)
  - Phone status classification (IVR vs Receptionist vs Voicemail)
  - Permutation bucketing and zero data loss
"""

from __future__ import annotations
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.create_tables import Base, Business, Contact
from app.pipeline.export_saleshandy import (
    classify_trade,
    classify_persona,
    classify_phone_type,
    sort_database_into_12_buckets,
    export_12_saleshandy_permutations,
)


# ---------------------------------------------------------------------------
# Test Suite 1: Trade Classification
# ---------------------------------------------------------------------------
class TestTradeClassification:

    def test_hvac_category_keywords(self):
        biz = Business(category="HVAC contractor, Air conditioning repair service", business_name="Cooling Experts")
        assert classify_trade(biz) == "HVAC"

    def test_plumbing_category_keywords(self):
        biz = Business(category="Plumber, Drainage service", business_name="San Jose Drain")
        assert classify_trade(biz) == "Plumbing"

    def test_dual_trade_primary_category_priority(self):
        biz = Business(category="Plumber", business_name="Mr. Reliable Plumbing & Heating", description="HVAC and Plumbing")
        assert classify_trade(biz) == "Plumbing"

    def test_fallback_trade_on_empty_metadata(self):
        biz = Business(category="", business_name="Generic Contracting Services", description="")
        assert classify_trade(biz) == "Plumbing"


# ---------------------------------------------------------------------------
# Test Suite 2: Persona Classification
# ---------------------------------------------------------------------------
class TestPersonaClassification:

    @pytest.mark.parametrize("email", [
        "info@company.com",
        "office@plumber.com",
        "contact@hvac.com",
        "admin@service.com",
        "support@contractor.com",
        "service@eliterooter.com",
        "privacy@benfranklin.com",
    ])
    def test_generic_email_prefixes_override_to_nonowner(self, email):
        contact = Contact(name="John Smith", title="Owner", email=email)
        assert classify_persona(contact) == "NonOwner"

    @pytest.mark.parametrize("name", [
        "Info/Office",
        "Decision Maker",
        "Office Team",
    ])
    def test_generic_name_identifiers_override_to_nonowner(self, name):
        contact = Contact(name=name, title="General Contact", email="john@company.com")
        assert classify_persona(contact) == "NonOwner"

    @pytest.mark.parametrize("title", [
        "Owner",
        "Founder & CEO",
        "President",
        "Partner",
        "Principal / Operator",
        "Executive / Decision Maker",
    ])
    def test_executive_job_titles_classify_as_owner(self, title):
        contact = Contact(name="Fred Daoud", title=title, email="fred@gogorooter.com")
        assert classify_persona(contact) == "Owner"

    def test_real_first_and_last_names_classify_as_owner(self):
        contact = Contact(name="Matthew Gjers", title="Service Professional", email="mgjers@goallstar.com")
        assert classify_persona(contact) == "Owner"


# ---------------------------------------------------------------------------
# Test Suite 3: Phone Destination Classification
# ---------------------------------------------------------------------------
class TestPhoneClassification:

    def test_ivr_status_mapping(self):
        contact = Contact(lead_status="Classified_IVR")
        assert classify_phone_type(contact) == "IVR"

    def test_receptionist_status_mapping(self):
        contact = Contact(lead_status="Classified_Receptionist")
        assert classify_phone_type(contact) == "Receptionist"

    def test_human_status_mapping(self):
        contact = Contact(lead_status="Human_Answered")
        assert classify_phone_type(contact) == "Receptionist"

    def test_voicemail_status_mapping(self):
        contact = Contact(lead_status="Classified_Voicemail")
        assert classify_phone_type(contact) == "Voicemail"

    @pytest.mark.parametrize("status", ["Not Contacted", "Verified", "Pending_Classification", ""])
    def test_fallback_phone_status_mapping(self, status):
        contact = Contact(lead_status=status)
        assert classify_phone_type(contact) == "Voicemail"


# ---------------------------------------------------------------------------
# Test Suite 4: Permutation Bucketing & Zero Data Loss
# ---------------------------------------------------------------------------
class TestBucketingAndDataConservation:

    @pytest.fixture
    def in_memory_session(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()

    def test_12_permutation_buckets_coverage_and_zero_data_loss(self, in_memory_session):
        session = in_memory_session

        # Create sample businesses for Owners and NonOwners
        b_hvac_owner = Business(id=1, business_name="Apex HVAC", category="HVAC contractor")
        b_plumb_owner = Business(id=2, business_name="Apex Plumbing", category="Plumber")
        b_hvac_team = Business(id=3, business_name="Metro HVAC Team", category="HVAC contractor")
        b_plumb_team = Business(id=4, business_name="Metro Plumbing Team", category="Plumber")
        session.add_all([b_hvac_owner, b_plumb_owner, b_hvac_team, b_plumb_team])
        session.commit()

        # Seed 12 contacts across all 12 permutations
        sample_contacts = [
            # HVAC Owner
            Contact(business_id=1, name="John Smith", title="Owner", email="john@apexhvac.com", lead_status="Classified_IVR"),
            Contact(business_id=1, name="Jane Doe", title="CEO", email="jane@apexhvac.com", lead_status="Classified_Receptionist"),
            Contact(business_id=1, name="Bob Builder", title="President", email="bob@apexhvac.com", lead_status="Classified_Voicemail"),
            # HVAC NonOwner
            Contact(business_id=3, name="Info/Office", title="General Contact", email="info@metrohvac.com", lead_status="Classified_IVR"),
            Contact(business_id=3, name="Office Team", title="General Contact", email="office@metrohvac.com", lead_status="Classified_Receptionist"),
            Contact(business_id=3, name="Service Dept", title="General Contact", email="service@metrohvac.com", lead_status="Classified_Voicemail"),
            # Plumbing Owner
            Contact(business_id=2, name="Mario Bros", title="Owner", email="mario@apexplumb.com", lead_status="Classified_IVR"),
            Contact(business_id=2, name="Luigi Bros", title="Partner", email="luigi@apexplumb.com", lead_status="Classified_Receptionist"),
            Contact(business_id=2, name="Princess Peach", title="Founder", email="peach@apexplumb.com", lead_status="Classified_Voicemail"),
            # Plumbing NonOwner
            Contact(business_id=4, name="Info/Office", title="General Contact", email="info@metroplumb.com", lead_status="Classified_IVR"),
            Contact(business_id=4, name="Office Team", title="General Contact", email="office@metroplumb.com", lead_status="Classified_Receptionist"),
            Contact(business_id=4, name="Support Dept", title="General Contact", email="support@metroplumb.com", lead_status="Classified_Voicemail"),
        ]
        session.add_all(sample_contacts)
        session.commit()

        # Execute sorter
        buckets = sort_database_into_12_buckets(session)

        # Assert all 12 keys exist
        assert len(buckets) == 12

        # Assert each bucket has exactly 1 contact
        for perm_tag, records in buckets.items():
            assert len(records) == 1, f"Expected 1 contact in {perm_tag}, found {len(records)}"

        # Assert total bucketed == total seeded (Zero Data Loss)
        total_bucketed = sum(len(r) for r in buckets.values())
        assert total_bucketed == 12

    def test_custom_variable_field_formatting(self, in_memory_session):
        session = in_memory_session
        biz = Business(id=1, business_name="Cooling Corp", category="HVAC contractor")
        contact = Contact(business_id=1, name="Alice Cooper", title="Owner", email="alice@cooling.com", lead_status="Classified_IVR")
        session.add_all([biz, contact])
        session.commit()

        buckets = sort_database_into_12_buckets(session)
        row = buckets["HVAC_Owner_IVR"][0]

        assert row["First Name"] == "Alice"
        assert row["Last Name"] == "Cooper"
        assert row["Email"] == "alice@cooling.com"
        assert row["Company"] == "Cooling Corp"
        assert row["Trade"] == "HVAC"
        assert row["Persona"] == "Owner"
        assert row["Phone Classification"] == "IVR"
        assert row["Permutation Tag"] == "HVAC_Owner_IVR"
        assert row["Demo Phone"] == "472-244-1040"

    def test_disconnected_phone_excluded(self, in_memory_session):
        session = in_memory_session
        biz = Business(id=1, business_name="Dead Phone HVAC", category="HVAC contractor")
        contact = Contact(business_id=1, name="Bob Builder", title="Owner", email="bob@deadphone.com", lead_status="Disconnected_Line")
        session.add_all([biz, contact])
        session.commit()

        buckets = sort_database_into_12_buckets(session)
        total_bucketed = sum(len(r) for r in buckets.values())
        assert total_bucketed == 0

    def test_min_score_gating(self, in_memory_session):
        from app.db.create_tables import EmailVerification

        session = in_memory_session
        biz = Business(id=1, business_name="Score Test HVAC", category="HVAC contractor")
        c1 = Contact(id=201, business_id=1, name="High Score", title="Owner", email="high@score.com")
        c2 = Contact(id=202, business_id=1, name="Low Score", title="Owner", email="low@score.com")
        ev1 = EmailVerification(contact_id=201, status="safe", score=90)
        ev2 = EmailVerification(contact_id=202, status="risky", score=20)
        session.add_all([biz, c1, c2, ev1, ev2])
        session.commit()

        buckets = sort_database_into_12_buckets(session, min_score=50)
        total_bucketed = sum(len(r) for r in buckets.values())
        assert total_bucketed == 1
        assert buckets["HVAC_Owner_Voicemail"][0]["Email"] == "high@score.com"

    def test_exclude_unexported(self, in_memory_session):
        from app.db.create_tables import ExportHistory
        from datetime import datetime, timezone

        session = in_memory_session
        biz = Business(id=1, business_name="Exported HVAC", category="HVAC contractor")
        c1 = Contact(id=101, business_id=1, name="Exported Guy", title="Owner", email="exported@hvac.com")
        c2 = Contact(id=102, business_id=1, name="Fresh Guy", title="Owner", email="fresh@hvac.com")
        eh = ExportHistory(contact_id=101, destination="saleshandy", exported_at=datetime.now(timezone.utc))
        session.add_all([biz, c1, c2, eh])
        session.commit()

        buckets = sort_database_into_12_buckets(session, exclude_unexported=True)
        total_bucketed = sum(len(r) for r in buckets.values())
        assert total_bucketed == 1
        assert buckets["HVAC_Owner_Voicemail"][0]["Email"] == "fresh@hvac.com"

    def test_suppressed_role_email_prefixes(self, in_memory_session):
        from app.pipeline.export_saleshandy import classify_persona
        c1 = Contact(name="Careers", title="General Contact", email="careers@biz.com")
        c2 = Contact(name="Billing Dept", title="General Contact", email="billing@biz.com")
        c3 = Contact(name="Info Office", title="General Contact", email="info@biz.com")

        assert classify_persona(c1) is None
        assert classify_persona(c2) is None
        assert classify_persona(c3) == "NonOwner"

    def test_company_name_cleaning(self):
        from app.pipeline.export_saleshandy import clean_company_name
        assert clean_company_name("Apex Plumbing & Drain Repair Inc.") == "Apex Plumbing & Drain Repair"
        assert clean_company_name("ABC Heating LLC | 24/7 Emergency Service") == "ABC Heating"
        assert clean_company_name("Cooling Corp - San Jose Branch") == "Cooling Corp"
        assert clean_company_name("") == "your business"

    def test_persona_priority_owner_over_nonowner(self, in_memory_session):
        session = in_memory_session
        biz = Business(id=10, business_name="Apex Plumbing LLC", category="Plumber")
        c1 = Contact(id=301, business_id=10, name="Mario Plumber", title="Owner", email="mario@apex.com")
        c2 = Contact(id=302, business_id=10, name="Info Office", title="General", email="info@apex.com")
        session.add_all([biz, c1, c2])
        session.commit()

        buckets = sort_database_into_12_buckets(session)
        total_bucketed = sum(len(r) for r in buckets.values())
        # Exactly 1 contact bucketed (Owner only, NonOwner skipped for same business)
        assert total_bucketed == 1
        assert buckets["Plumbing_Owner_Voicemail"][0]["Email"] == "mario@apex.com"
        assert buckets["Plumbing_Owner_Voicemail"][0]["Company"] == "Apex Plumbing"

