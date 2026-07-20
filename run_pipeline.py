"""
End-to-end lead-gen pipeline orchestrator.

Stages:
    1. Scrape Google Maps → raw_leads
    2. Clean/dedupe → businesses + contacts
    3. Crawl business websites → email contacts
    4. Loop 1-3 at increasing scraper depth until min_contacts hit
    5. Export new leads to Sheets (or CSV fallback)
"""

import sys

from sqlalchemy.orm import sessionmaker

from app.db.create_tables import Contact, init_db
from app.db.database import engine
from app.logging_config import get_logger, setup_logging
from app.pipeline.export_sheets import export_new_leads
from app.pipeline.extract_emails import harvest_emails_from_websites
from app.pipeline.process_leads import process_and_deduplicate_leads
from app.scraper.run_scraper import execute_scrape_and_ingest, geocode_location

logger = get_logger(__name__)


def get_contact_count() -> int:
    """Count total contacts in the DB."""
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        return session.query(Contact).count()
    finally:
        session.close()


def run_end_to_end_pipeline(query: str, location: str, min_contacts: int = 500) -> None:
    """
    Orchestrate the pipeline (see module docstring). Loops the scraper at
    growing depth until the DB reaches `min_contacts` or hits max_depth.
    """
    setup_logging()

    logger.info("=" * 60)
    logger.info("STARTING END-TO-END LEAD GENERATION PIPELINE")
    logger.info("query=%r location=%r min_contacts=%d", query, location, min_contacts)
    logger.info("=" * 60)

    # Bootstrap schema once per run (idempotent — no-op if tables exist).
    init_db()

    # Geocode ONCE up front — Nominatim ToS asks for max 1 req/sec and no
    # duplicate work; the loop below reuses these coords for every scrape.
    lat, lon = geocode_location(location)
    if lat is None or lon is None:
        logger.warning(
            "Could not geocode %r. Scraper will retry per iteration.", location
        )

    depth = 1
    max_depth = 20

    try:
        while True:
            logger.info("--- Running scraping loop (depth=%d) ---", depth)
            # Step 1: scrape (using cached lat/lon)
            execute_scrape_and_ingest(query, location, lat=lat, lon=lon, depth=depth)

            # Step 2: clean + dedupe
            process_and_deduplicate_leads()

            # Step 3: crawl websites for direct emails
            harvest_emails_from_websites()

            current_contacts = get_contact_count()
            logger.info(
                "Current contacts in DB: %d / %d", current_contacts, min_contacts
            )

            if current_contacts >= min_contacts:
                logger.info("Reached the target of %d contacts.", min_contacts)
                break

            if depth >= max_depth:
                logger.warning(
                    "Reached max scraper depth (%d) but only %d/%d contacts. Stopping.",
                    max_depth, current_contacts, min_contacts,
                )
                break

            depth += 2
            logger.info(
                "Not enough contacts yet. Increasing scraper depth to %d.", depth
            )

        # Step 4: export
        export_new_leads()
        logger.info("=" * 60)
        logger.info("PIPELINE EXECUTED SUCCESSFULLY")
        logger.info("=" * 60)

    except Exception as e:
        logger.exception("Pipeline run aborted: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    # Default search: Plumbing in San Francisco, CA
    run_end_to_end_pipeline(
        query="Plumbing",
        location="San Francisco, CA",
        min_contacts=500,
    )
