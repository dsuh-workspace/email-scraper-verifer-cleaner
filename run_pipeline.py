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

# Default query variants for full-harvest multi-query pass. Chosen from the
# 2026-07-20 SJ experiment — "Leak repair" alone added 50 unique businesses
# no other query surfaced, so the list favors breadth over redundancy.
DEFAULT_HARVEST_QUERIES = (
    "Plumbing",
    "Plumber",
    "Plumbing services",
    "Emergency plumber",
    "Drain cleaning",
    "Water heater repair",
    "Leak repair",
    "Sewer service",
)


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
    parser.add_argument(
        "--strategy",
        choices=["single-centroid", "grid", "full-harvest"],
        default=None,
        help=(
            "Scrape strategy. 'single-centroid' (legacy depth loop), "
            "'grid' (== --grid), 'full-harvest' (grid + multi-query slow + "
            "fast ZIP top-up; empirically 39%% more coverage than grid alone). "
            "Defaults to 'grid' if --grid set, else 'single-centroid'."
        ),
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=None,
        help=(
            "Comma-separated query variants for the multi-query pass "
            "(full-harvest strategy only). Defaults to 8 plumbing variants."
        ),
    )
    parser.add_argument(
        "--zip-csv",
        type=str,
        default=None,
        help=(
            "Path to CSV with 'zip,city,state' columns for the fast ZIP "
            "top-up pass (full-harvest strategy). Optional; ZIP pass is "
            "skipped if omitted."
        ),
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


def _load_zip_csv(path: str) -> list[dict[str, str]]:
    """Load ZIP top-up rows: expect 'zip,city,state' columns."""
    import csv as _csv
    with open(path, "r", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for row in reader:
            z = (row.get("zip") or "").strip()
            city = (row.get("city") or "").strip()
            state = (row.get("state") or "").strip()
            if not z:
                continue
            rows.append({"zip": z, "city": city, "state": state})
    return rows


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
    strategy: str = "single-centroid",
    queries: tuple[str, ...] | None = None,
    zip_csv: str | None = None,
) -> None:
    """
    Orchestrate pipeline. Three strategies:

    - single-centroid (legacy): loop at increasing depths until
      min_contacts hit or max_depth reached.
    - grid: one scrape iterates cells over the location's bounding box
      (Nominatim-derived, or explicit `bbox` arg). No depth loop. Grid+d3
      is empirically 4-25x richer than a curated ZIP sweep.
    - full-harvest: grid + multi-query slow at centroid + optional fast
      ZIP top-up. Empirically 39% more coverage than grid alone
      (SJ 2026-07-20: grid=362, +multi-query=473, +fast-ZIP=504).
    """
    setup_logging()

    logger.info("=" * 60)
    logger.info("STARTING END-TO-END LEAD GENERATION PIPELINE")
    logger.info(
        "query=%r location=%r min_contacts=%d strategy=%s",
        query,
        location,
        min_contacts,
        strategy,
    )
    logger.info("=" * 60)

    # Back-compat: callers passing use_grid=True keep working even if they
    # didn't set strategy explicitly.
    if use_grid and strategy == "single-centroid":
        strategy = "grid"

    init_db()

    try:
        lat, lon, geo_bbox = geocode_location(location)
        if lat is None or lon is None:
            logger.warning("Could not geocode %r. Scraper will retry per iteration.", location)

        if strategy == "grid":
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
                disable_proxy=disable_scraper_proxy,
            )
            process_and_deduplicate_leads()
            harvest_emails_from_websites(disable_proxy=disable_crawler_proxy)
            current_contacts = get_contact_count()
            logger.info(
                "Grid scrape complete. Contacts in DB: %d (target %d).",
                current_contacts,
                min_contacts,
            )
        elif strategy == "full-harvest":
            effective_bbox = bbox if bbox is not None else geo_bbox
            if effective_bbox is None:
                raise RuntimeError(
                    f"full-harvest requires a bounding box for the grid pass. "
                    f"Nominatim returned none for {location!r}; supply --bbox."
                )
            query_variants = list(queries or DEFAULT_HARVEST_QUERIES)

            # Pass 1 — grid over bbox (single query, JS mode, depth 3).
            logger.info("--- Full-harvest PASS 1: grid (bbox=%s cell_km=%.2f) ---",
                        effective_bbox, cell_km)
            execute_scrape_and_ingest(
                query,
                location,
                bbox=effective_bbox,
                cell_km=cell_km,
                depth=3,
                disable_proxy=disable_scraper_proxy,
            )
            process_and_deduplicate_leads()

            # Pass 2 — multi-query slow at centroid (browser reuses context
            # across the 8 queries; Am beats N-separate As runs on wall time).
            if lat is not None and lon is not None:
                logger.info("--- Full-harvest PASS 2: multi-query slow at centroid ---")
                execute_scrape_and_ingest(
                    query,
                    location,
                    lat=lat,
                    lon=lon,
                    depth=10,
                    queries=query_variants,
                    fast_mode=False,
                    disable_proxy=disable_scraper_proxy,
                )
                process_and_deduplicate_leads()
            else:
                logger.warning("Skipping PASS 2 — no centroid available for %r.", location)

            # Pass 3 — fast ZIP top-up (optional). Cheap, ~2s per ZIP.
            if zip_csv:
                logger.info("--- Full-harvest PASS 3: fast ZIP top-up from %s ---", zip_csv)
                zip_rows = _load_zip_csv(zip_csv)
                for i, row in enumerate(zip_rows, 1):
                    zip_loc = ", ".join(x for x in (row["city"], row["state"], row["zip"]) if x)
                    zlat, zlon, _ = geocode_location(zip_loc) if zip_loc else (None, None, None)
                    if zlat is None or zlon is None:
                        logger.warning("  [%d/%d zip %s] geocode failed, skipping.",
                                       i, len(zip_rows), row["zip"])
                        continue
                    execute_scrape_and_ingest(
                        query,
                        zip_loc,
                        lat=zlat,
                        lon=zlon,
                        depth=3,
                        fast_mode=True,
                        disable_proxy=disable_scraper_proxy,
                    )
                process_and_deduplicate_leads()
            else:
                logger.info("--- Full-harvest PASS 3: skipped (no --zip-csv) ---")

            harvest_emails_from_websites(disable_proxy=disable_crawler_proxy)
            current_contacts = get_contact_count()
            logger.info(
                "Full-harvest complete. Contacts in DB: %d (target %d).",
                current_contacts,
                min_contacts,
            )
        else:
            # single-centroid legacy
            depth = 1
            while True:
                logger.info("--- Running scraping loop (depth=%d) ---", depth)
                execute_scrape_and_ingest(
                    query, location, lat=lat, lon=lon, depth=depth,
                    disable_proxy=disable_scraper_proxy,
                )
                process_and_deduplicate_leads()
                harvest_emails_from_websites(disable_proxy=disable_crawler_proxy)

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



def _resolve_strategy(args: argparse.Namespace) -> str:
    """Resolve strategy from CLI flags. Explicit --strategy wins."""
    if args.strategy:
        return args.strategy
    if args.grid:
        return "grid"
    return "single-centroid"


def main() -> None:
    args = parse_args()
    bbox = _parse_bbox(args.bbox) if args.bbox else None
    strategy = _resolve_strategy(args)

    if bbox is not None and strategy not in ("grid", "full-harvest"):
        logger.warning("--bbox supplied but strategy is %s; bbox will be ignored.", strategy)
    if args.zip_csv and strategy != "full-harvest":
        logger.warning("--zip-csv supplied but strategy is %s; zip-csv ignored.", strategy)
    if args.queries and strategy != "full-harvest":
        logger.warning("--queries supplied but strategy is %s; queries ignored.", strategy)

    disable_scraper_proxy = args.no_proxy or args.no_scraper_proxy
    disable_crawler_proxy = args.no_proxy or args.no_crawler_proxy

    query_variants: tuple[str, ...] | None = None
    if args.queries:
        variants = tuple(q.strip() for q in args.queries.split(",") if q.strip())
        query_variants = variants or None

    run_end_to_end_pipeline(
        query=args.query,
        location=args.location,
        min_contacts=args.min_contacts,
        max_depth=args.max_depth,
        use_grid=(strategy == "grid"),
        cell_km=args.cell_km,
        bbox=bbox,
        disable_scraper_proxy=disable_scraper_proxy,
        disable_crawler_proxy=disable_crawler_proxy,
        strategy=strategy,
        queries=query_variants,
        zip_csv=args.zip_csv,
    )


if __name__ == "__main__":
    main()
