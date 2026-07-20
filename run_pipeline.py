"""
End-to-end lead-gen pipeline orchestrator.

Stages:
    1. Scrape Google Maps → raw_leads
    2. Clean/dedupe → businesses + contacts
    3. Crawl business websites → email contacts
    4. Loop 1-3 at increasing scraper depth until min_contacts hit
    5. Export new leads to Sheets (or CSV fallback)
"""

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from app.db.create_tables import Contact, ExportHistory, init_db
from app.db.database import engine
from app.logging_config import get_logger, setup_logging
from app.pipeline.export_sheets import export_new_leads
from app.pipeline.extract_emails import harvest_emails_from_websites
from app.pipeline.process_leads import process_and_deduplicate_leads
from app.scraper.run_scraper import execute_scrape_and_ingest, geocode_location

logger = get_logger(__name__)
Session = sessionmaker(bind=engine)
LEGACY_EXPORT_DESTINATION = "local_csv_leads"


@dataclass(frozen=True)
class LocationRunMetrics:
    depths_run: tuple[int, ...]
    final_depth: int
    total_contacts: int
    exportable_contacts: int
    baseline_exportable_contacts: int
    new_exportable_contacts: int
    stale_iterations: int


def get_contact_count() -> int:
    """Count total contacts in DB."""
    session = Session()
    try:
        return session.query(Contact).count()
    finally:
        session.close()



def get_exportable_contact_count(destination: str = LEGACY_EXPORT_DESTINATION) -> int:
    """Count contacts that have not yet been exported to destination."""
    session = Session()
    try:
        return session.query(Contact).filter(
            ~Contact.id.in_(
                session.query(ExportHistory.contact_id).filter(
                    ExportHistory.destination == destination
                )
            )
        ).count()
    finally:
        session.close()



def run_location_pipeline(
    query: str,
    location: str,
    max_depth: int,
    target_new_exportable: int | None = None,
    stale_iterations_limit: int | None = None,
    export_destination: str = LEGACY_EXPORT_DESTINATION,
) -> LocationRunMetrics:
    """Run scrape/process/harvest loop for one location and return metrics."""
    lat, lon = geocode_location(location)
    if lat is None or lon is None:
        logger.warning("Could not geocode %r. Scraper will retry per iteration.", location)

    baseline_exportable = get_exportable_contact_count(export_destination)
    depth = 1
    stale_iterations = 0
    depths_run: list[int] = []

    while True:
        logger.info("--- Running scraping loop (depth=%d) ---", depth)
        depths_run.append(depth)

        execute_scrape_and_ingest(query, location, lat=lat, lon=lon, depth=depth)
        process_and_deduplicate_leads()
        harvest_emails_from_websites()

        total_contacts = get_contact_count()
        exportable_contacts = get_exportable_contact_count(export_destination)
        new_exportable_contacts = max(0, exportable_contacts - baseline_exportable)

        logger.info(
            "Location %r now has %d total contacts and %d new exportable contacts.",
            location,
            total_contacts,
            new_exportable_contacts,
        )

        if target_new_exportable is not None and new_exportable_contacts >= target_new_exportable:
            logger.info(
                "Reached target of %d new exportable contacts for %r.",
                target_new_exportable,
                location,
            )
            return LocationRunMetrics(
                depths_run=depths_run,
                final_depth=depth,
                total_contacts=total_contacts,
                exportable_contacts=exportable_contacts,
                baseline_exportable_contacts=baseline_exportable,
                new_exportable_contacts=new_exportable_contacts,
                stale_iterations=stale_iterations,
            )

        if depth >= max_depth:
            logger.warning(
                "Reached max scraper depth (%d) for %r. Stopping.",
                max_depth,
                location,
            )
            return LocationRunMetrics(
                depths_run=depths_run,
                final_depth=depth,
                total_contacts=total_contacts,
                exportable_contacts=exportable_contacts,
                baseline_exportable_contacts=baseline_exportable,
                new_exportable_contacts=new_exportable_contacts,
                stale_iterations=stale_iterations,
            )

        if target_new_exportable is not None and stale_iterations_limit is not None:
            if new_exportable_contacts == 0:
                stale_iterations += 1
            else:
                stale_iterations = 0

            if stale_iterations >= stale_iterations_limit:
                logger.warning(
                    "No new exportable contacts for %d consecutive depth bumps at %r. Stopping.",
                    stale_iterations_limit,
                    location,
                )
                return LocationRunMetrics(
                    depths_run=tuple(depths_run),
                    final_depth=depth,
                    total_contacts=total_contacts,
                    exportable_contacts=exportable_contacts,
                    baseline_exportable_contacts=baseline_exportable,
                    new_exportable_contacts=new_exportable_contacts,
                    stale_iterations=stale_iterations,
                )

        depth += 2
        logger.info("Increasing scraper depth to %d.", depth)



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run end-to-end lead generation pipeline."
    )
    parser.add_argument("--query", required=True, help="Industry keyword to scrape")
    parser.add_argument(
        "--location", required=True, help="Location string for Google Maps search"
    )
    parser.add_argument(
        "--min-contacts",
        type=int,
        default=500,
        help="Stop once DB has at least this many contacts",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=20,
        help="Maximum scraper depth before stopping",
    )
    return parser.parse_args()



def run_end_to_end_pipeline(
    query: str,
    location: str,
    min_contacts: int = 500,
    max_depth: int = 20,
) -> None:
    """
    Orchestrate pipeline. Legacy stop condition uses total DB contacts.
    """
    setup_logging()

    logger.info("=" * 60)
    logger.info("STARTING END-TO-END LEAD GENERATION PIPELINE")
    logger.info("query=%r location=%r min_contacts=%d", query, location, min_contacts)
    logger.info("=" * 60)

    init_db()

    try:
        lat, lon = geocode_location(location)
        if lat is None or lon is None:
            logger.warning("Could not geocode %r. Scraper will retry per iteration.", location)

        depth = 1
        while True:
            logger.info("--- Running scraping loop (depth=%d) ---", depth)
            execute_scrape_and_ingest(query, location, lat=lat, lon=lon, depth=depth)
            process_and_deduplicate_leads()
            harvest_emails_from_websites()

            current_contacts = get_contact_count()
            logger.info("Current contacts in DB: %d / %d", current_contacts, min_contacts)

            if current_contacts >= min_contacts:
                logger.info("Reached target of %d contacts.", min_contacts)
                break

            if depth >= max_depth:
                logger.warning("Reached max scraper depth (%d). Stopping.", max_depth)
                break

            depth += 2
            logger.info("Increasing scraper depth to %d.", depth)

        export_new_leads()
        logger.info("=" * 60)
        logger.info("PIPELINE EXECUTED SUCCESSFULLY")
        logger.info("=" * 60)

    except Exception as e:
        logger.exception("Pipeline run aborted: %s", e)
        sys.exit(1)



def main() -> None:
    args = parse_args()
    run_end_to_end_pipeline(
        query=args.query,
        location=args.location,
        min_contacts=args.min_contacts,
        max_depth=args.max_depth,
    )


if __name__ == "__main__":
    main()
