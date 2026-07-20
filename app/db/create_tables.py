"""
SQLAlchemy models + schema bootstrap.

Constraints/indexes summary
---------------------------
- businesses.domain          UNIQUE  (dedupe key)
- (contacts.business_id, contacts.email)   composite UNIQUE   nullable — SQLite
                                                                treats NULLs as
                                                                distinct so
                                                                phone-only rows
                                                                still insert.
- (contacts.business_id, contacts.phone)   composite index (dup checks)
- (export_history.contact_id, destination) composite index (export filter)
- FK columns get an index individually so joins are cheap.

Call init_db() once at startup — do NOT invoke on module import.
"""

from sqlalchemy import (
    Column,
    Integer,
    Text,
    TIMESTAMP,
    ForeignKey,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import declarative_base

from app.db.database import engine

from app.logging_config import get_logger

logger = get_logger(__name__)


Base = declarative_base()

# ================================
# Table definitions
# ================================


class ScrapeRun(Base):
    """One row per invocation of the Google Maps scraper."""
    __tablename__ = "scrape_runs"

    id = Column(Integer, primary_key=True)
    query = Column(Text)
    location = Column(Text)
    category = Column(Text)
    status = Column(Text, index=True)
    started_at = Column(TIMESTAMP)
    completed_at = Column(TIMESTAMP)


class RawLead(Base):
    """Rough-draft dump of scraper output. Cleaned into Business/Contact."""
    __tablename__ = "raw_leads"

    id = Column(Integer, primary_key=True)

    scrape_run_id = Column(
        Integer, ForeignKey("scrape_runs.id"), index=True,
    )

    business_name = Column(Text)
    category = Column(Text)
    phone = Column(Text)
    website = Column(Text)
    email = Column(Text)

    # Stamped by process_leads after this raw row has been cleaned and
    # promoted into businesses/contacts. NULL means "still to process".
    # Indexed because process_leads filters on `IS NULL`.
    processed_at = Column(TIMESTAMP, index=True)


class Business(Base):
    """Cleaned, deduped master directory of unique companies."""
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True)
    business_name = Column(Text, index=True)
    category = Column(Text)
    website = Column(Text)
    # unique so dedupe is enforced at the DB layer, not just app layer.
    # Nullable — some raw leads have no website.
    domain = Column(Text, unique=True, index=True)
    phone = Column(Text, index=True)


class Contact(Base):
    """Individual person at a Business (owner, manager, generic info@, etc.)."""
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True)

    business_id = Column(
        Integer, ForeignKey("businesses.id"), index=True,
    )

    name = Column(Text)
    phone = Column(Text)
    title = Column(Text)
    email = Column(Text)
    lead_status = Column(Text, index=True)

    __table_args__ = (
        # Composite unique — one (biz, email) pair max. NULL emails are
        # allowed multiple times (SQLite + Postgres both treat NULL as
        # distinct for UNIQUE), which matches how we insert phone-only
        # placeholder contacts.
        UniqueConstraint("business_id", "email", name="uq_contact_biz_email"),
        Index("ix_contact_biz_phone", "business_id", "phone"),
    )


class EmailVerification(Base):
    """Cache of Reacher (or other) verification results per contact."""
    __tablename__ = "email_verifications"

    id = Column(Integer, primary_key=True)
    contact_id = Column(
        Integer, ForeignKey("contacts.id"), index=True,
    )

    status = Column(Text, index=True)
    score = Column(Integer)


class ExportHistory(Base):
    """Every time we push a contact to Sheets/CSV/CRM, log it here."""
    __tablename__ = "export_history"

    id = Column(Integer, primary_key=True)
    contact_id = Column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), index=True,
        nullable=False,
    )
    destination = Column(Text, index=True)
    exported_at = Column(
        TIMESTAMP,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        Index("ix_export_contact_dest", "contact_id", "destination"),
    )


# ==========================================
# EXECUTION (call explicitly, not on import)
# ==========================================


def init_db() -> None:
    """
    Create all tables if they don't exist. Idempotent.

    Called once from run_pipeline.py at startup. Do NOT invoke at
    module-import time — that fires every time a worker imports the
    models and can crash if DATABASE_URL is misconfigured.
    """
    Base.metadata.create_all(engine)
    logger.info("Database tables created (or already existed).")
if __name__ == "__main__":
    # Allow `python -m app.db.create_tables` to bootstrap the schema.
    init_db()