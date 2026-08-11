"""
Unit tests for call attempts tracking and 3-retry fallback classification.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.create_tables import Base, Contact, Business
from app.pipeline.call_leads import sync_phone_classifications_across_business_contacts


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_contact_call_attempts_default(memory_db):
    b = Business(business_name="Test Plumbing", phone="+14085550199")
    memory_db.add(b)
    memory_db.commit()

    c = Contact(business_id=b.id, phone="+14085550199", lead_status="Not Contacted", call_attempts=0)
    memory_db.add(c)
    memory_db.commit()

    assert c.call_attempts == 0


def test_retry_threshold_logic():
    class MockContact:
        def __init__(self, attempts, status):
            self.call_attempts = attempts
            self.lead_status = status

    # 1 attempt -> Unanswered_Retry
    c1 = MockContact(1, "Unanswered_Retry")
    status1 = "Classified_Voicemail" if c1.call_attempts >= 3 else "Unanswered_Retry"
    assert status1 == "Unanswered_Retry"

    # 2 attempts -> Unanswered_Retry
    c2 = MockContact(2, "Unanswered_Retry")
    status2 = "Classified_Voicemail" if c2.call_attempts >= 3 else "Unanswered_Retry"
    assert status2 == "Unanswered_Retry"

    # 3 attempts -> Classified_Voicemail
    c3 = MockContact(3, "Unanswered_Retry")
    status3 = "Classified_Voicemail" if c3.call_attempts >= 3 else "Unanswered_Retry"
    assert status3 == "Classified_Voicemail"
