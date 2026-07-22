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
            Contact.email.isnot(None),
            ~Contact.id.in_(
                session.query(ExportHistory.contact_id).filter(
                    ExportHistory.destination == destination,
                    ExportHistory.contact_id.isnot(None),
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
    disable_scraper_proxy: bool = False,
    disable_crawler_proxy: bool = False,
) -> LocationRunMetrics:
    """Run scrape/process/harvest loop for one location and return metrics."""
    lat, lon, _bbox = geocode_location(location)
    if lat is None or lon is None:
        logger.warning("Could not geocode %r. Scraper will retry per iteration.", location)

    baseline_exportable = get_exportable_contact_count(export_destination)
    depth = 1
    stale_iterations = 0
    depths_run: list[int] = []

    while True:
        logger.info("--- Running scraping loop (depth=%d) ---", depth)
        depths_run.append(depth)

        execute_scrape_and_ingest(
            query,
            location,
            lat=lat,
            lon=lon,
            depth=depth,
            disable_proxy=disable_scraper_proxy,
        )
        process_and_deduplicate_leads()
        harvest_emails_from_websites(disable_proxy=disable_crawler_proxy)

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
                depths_run=tuple(depths_run),
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
                depths_run=tuple(depths_run),
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
        help="Maximum scraper depth before stopping (single-centroid mode only)",
    )
    parser.add_argument(
        "--grid",
        action="store_true",
        help=(
            "Use native grid-mode scraping (JS-mode, requires Playwright). "
            "One scrape iterates cells over the location's bounding box. "
            "Empirically 4-25x higher coverage than single-centroid mode."
        ),
    )
    parser.add_argument(
        "--cell-km",
        type=float,
        default=2.0,
        help="Grid cell size in km (default 2.0). Ignored without --grid.",
    )
    parser.add_argument(
        "--bbox",
        type=str,
        default=None,
        help=(
            "Explicit bounding box 'min_lat,min_lon,max_lat,max_lon' for "
            "grid mode. Overrides Nominatim-derived bbox. Ignored without --grid."
        ),
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable scraper and crawler proxy usage for this run.",
    )
    parser.add_argument(
        "--no-scraper-proxy",
        action="store_true",
        help="Disable scraper proxy usage for this run.",
    )
    parser.add_argument(
        "--no-crawler-proxy",
        action="store_true",
        help="Disable crawler proxy usage for this run.",
    )
    return parser.parse_args()



def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"--bbox must be 'min_lat,min_lon,max_lat,max_lon' (4 floats), got {raw!r}"
        )
    try:
        min_lat, min_lon, max_lat, max_lon = (float(p) for p in parts)
    except ValueError as e:
        raise ValueError(f"--bbox values must be floats: {e}") from e
    if not (min_lat < max_lat and min_lon < max_lon):
        raise ValueError(
            f"--bbox must satisfy min_lat<max_lat and min_lon<max_lon, got {raw!r}"
        )
    return (min_lat, min_lon, max_lat, max_lon)


def run_end_to_end_pipeline(
    query: str,
    location: str,
    min_contacts: int = 500,
    max_depth: int = 20,
    use_grid: bool = False,
    cell_km: float = 2.0,
    bbox: tuple[float, float, float, float] | None = None,
    disable_scraper_proxy: bool = False,
    disable_crawler_proxy: bool = False,
) -> None:
    """
    Orchestrate pipeline.

    Two modes:
    - Single-centroid (default): loop scraper at increasing depths until
      min_contacts hit or max_depth reached. Legacy behavior.
    - Grid mode (use_grid=True): one scrape iterates cells over the
      location's bounding box (Nominatim-derived, or explicit `bbox` arg).
      No depth loop — grid+depth 3 was empirically 4-25x richer than a
      curated ZIP sweep. If min_contacts isn't hit after the grid scrape,
      we still stop; grid coverage is the ceiling.
    """
    setup_logging()

    logger.info("=" * 60)
    logger.info("STARTING END-TO-END LEAD GENERATION PIPELINE")
    logger.info(
        "query=%r location=%r min_contacts=%d mode=%s",
        query,
        location,
        min_contacts,
        "grid" if use_grid else "single-centroid",
    )
    logger.info("=" * 60)

    init_db()

    try:
        lat, lon, geo_bbox = geocode_location(location)
        if lat is None or lon is None:
            logger.warning("Could not geocode %r. Scraper will retry per iteration.", location)

        if use_grid:
            effective_bbox = bbox if bbox is not None else geo_bbox
            if effective_bbox is None:
                raise RuntimeError(
                    f"Grid mode requires a bounding box. Nominatim returned none "
                    f"for {location!r} and no --bbox override was supplied."
                )
            logger.info(
                "--- Grid scrape (bbox=%s cell_km=%.2f) ---",
                effective_bbox,
                cell_km,
            )
            execute_scrape_and_ingest(
                query,
                location,
                bbox=effective_bbox,
                cell_km=cell_km,
                depth=3,
            )
            process_and_deduplicate_leads()
            harvest_emails_from_websites()
            current_contacts = get_contact_count()
            logger.info(
                "Grid scrape complete. Contacts in DB: %d (target %d).",
                current_contacts,
                min_contacts,
            )
        else:
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
    bbox = _parse_bbox(args.bbox) if args.bbox else None
    if bbox is not None and not args.grid:
        logger.warning("--bbox supplied without --grid; bbox will be ignored.")
    disable_scraper_proxy = args.no_proxy or args.no_scraper_proxy
    disable_crawler_proxy = args.no_proxy or args.no_crawler_proxy

    run_end_to_end_pipeline(
        query=args.query,
        location=args.location,
        min_contacts=args.min_contacts,
        max_depth=args.max_depth,
        use_grid=args.grid,
        cell_km=args.cell_km,
        bbox=bbox,
        disable_scraper_proxy=disable_scraper_proxy,
        disable_crawler_proxy=disable_crawler_proxy,
    )


if __name__ == "__main__":
    main()
