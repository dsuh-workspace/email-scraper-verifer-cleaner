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

import logging

from sqlalchemy import (
    Column,
    Integer,
    Text,
    TIMESTAMP,
    ForeignKey,
    Index,
    UniqueConstraint,
    inspect,
    text,
    Float
)
from sqlalchemy.orm import declarative_base

from app.db.database import engine

logger = logging.getLogger(__name__)


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
    review_count = Column(Integer)
    review_rating = Column(Float)
    address = Column(Text)
    status = Column(Text)
    description = Column(Text)
    place_id = Column(Text)

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
    review_count = Column(Integer)
    review_rating = Column(Float)
    address = Column(Text)
    status = Column(Text)
    description = Column(Text)
    place_id = Column(Text)
    first_scrape_run_id = Column(Integer, ForeignKey("scrape_runs.id"), index=True)

    # --- crawl-attempt ledger (review #R7) ---------------------------------
    # Without these, a business we crawled that yielded no email is
    # indistinguishable from one never crawled: harvest_emails_from_websites()
    # builds its skip-set from contacts with a non-null email, so email-less
    # sites got re-fetched on every depth iteration and every re-run.
    #
    # last_crawled_at: stamped on every crawl attempt, success or not.
    # crawl_attempts:  count of *consecutive* attempts that produced no
    #   email. Reset to 0 the moment a crawl yields one, so a site that
    #   starts publishing an address isn't stuck at the give-up threshold.
    #
    # Deliberately unindexed: harvest_emails_from_websites() loads the
    # website-having businesses with one SELECT and splits them in Python
    # (matching the existing `.all()` + comprehension shape), so no query
    # filters on these. An index here would only cost write time on every
    # stamp. It would also silently diverge between a fresh DB and a
    # migrated one, since create_all() adds indexes to new tables only.
    last_crawled_at = Column(TIMESTAMP)
    crawl_attempts = Column(Integer, nullable=False, server_default=text("0"))


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
    first_scrape_run_id = Column(Integer, ForeignKey("scrape_runs.id"), index=True)
    call_attempts = Column(Integer, nullable=False, server_default=text("0"))

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


# Columns added after the initial schema shipped. `create_all()` creates
# missing *tables* but never alters an existing one, so a DB created before
# these columns existed would raise "no such column" on the first query
# that mentions them. Each entry is (table, column, DDL type clause) and is
# applied only when absent, so this stays idempotent and cheap.
_ADDITIVE_COLUMNS = (
    ("businesses", "last_crawled_at", "TIMESTAMP"),
    ("businesses", "crawl_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("businesses", "first_scrape_run_id", "INTEGER"),
    ("contacts", "first_scrape_run_id", "INTEGER"),
    ("contacts", "call_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("export_history", "exported_at", "TIMESTAMP"),
)


def _apply_additive_columns() -> None:
    """Add post-hoc columns that an older DB file predates.

    Deliberately narrow: additive, nullable-or-defaulted columns only. This
    is not a migration framework — anything that drops, renames, or
    backfills still belongs in the documented SQL in MAINTENANCE_SQL.md.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue  # create_all() just built it with the column present.
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        logger.info("Added missing column %s.%s (%s).", table, column, ddl_type)


def init_db() -> None:
    """
    Create all tables if they don't exist, then add any missing additive
    columns. Idempotent.

    Called once from run_pipeline.py at startup. Do NOT invoke at
    module-import time — that fires every time a worker imports the
    models and can crash if DATABASE_URL is misconfigured.
    """
    Base.metadata.create_all(engine)
    _apply_additive_columns()
    logger.info("Database tables created (or already existed).")
if __name__ == "__main__":
    # Allow `python -m app.db.create_tables` to bootstrap the schema.
    init_db()