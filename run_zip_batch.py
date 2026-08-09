"""Batch zip-code runner with per-zip new-exportable gating."""

from __future__ import annotations
import argparse
import csv
import logging
from pathlib import Path

from app.db.create_tables import init_db
from app.logging_config import setup_logging
from app.pipeline.export_sheets import export_run_outputs
from app.scraper.pacing import pace
from app.scraper.run_scraper import geocode_location
# _resolve_strategy / _resolve_query_variants are shared rather than
# reimplemented: both CLIs must agree on what --grid means and on when a
# full-harvest is refused for lacking a variant set, since that check is the
# difference between a full sweep and grid-level results at full wall cost.
from run_pipeline import (
    DEFAULT_CELL_KM,
    DEFAULT_MAX_DEPTH,
    _default_csv_path,
    _resolve_query_variants,
    _resolve_strategy,
    run_location_full_harvest,
    run_location_grid,
    run_location_pipeline,
)

logger = logging.getLogger(__name__)

STRATEGIES = ("single-centroid", "grid", "full-harvest")

# Per-ZIP depth-loop defaults. The flags themselves default to None so "did the
# operator pass this?" is never inferred from comparing against a literal —
# same reasoning as run_pipeline.py's DEFAULT_MIN_CONTACTS. The batch wants a
# much lower per-ZIP target than a single metro run (nearby ZIPs overlap, so
# marginal yield per ZIP is small), but the depth cap and grid cell size are
# properties of the shared depth loop and grid, so both come from run_pipeline
# (DEFAULT_MAX_DEPTH, DEFAULT_CELL_KM).
DEFAULT_TARGET_NEW_EXPORTABLE = 20
DEFAULT_STALE_ITERATIONS = 2



def _row_location(row: dict[str, str]) -> str | None:
    location = (row.get("location") or "").strip()
    if location:
        return location

    zip_code = (row.get("zip") or "").strip()
    city = (row.get("city") or "").strip()
    state = (row.get("state") or "").strip()

    if zip_code and city and state:
        return f"{city}, {state} {zip_code}"
    if zip_code and state:
        return f"{zip_code}, {state}"
    if zip_code:
        return zip_code
    return None



def load_locations(zip_file: str) -> list[str]:
    with Path(zip_file).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Zip file must have a header row.")

        locations = []
        for index, row in enumerate(reader, start=2):
            location = _row_location(row)
            if location is None:
                logger.warning("Skipping row %d with no usable location fields: %s", index, row)
                continue
            locations.append(location)

    if not locations:
        raise ValueError("Zip file contained no usable rows.")

    return locations



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run batch lead generation from CSV of zip codes/locations."
    )
    parser.add_argument("--query", required=True, help="Industry keyword to scrape")
    parser.add_argument("--zip-file", required=True, help="CSV file of zip/location rows")
    parser.add_argument(
        "--strategy",
        choices=STRATEGIES,
        default=None,
        help=(
            "Scrape strategy applied to every row. Default single-centroid. "
            "grid/full-harvest resolve each row's bounding box from Nominatim."
        ),
    )
    parser.add_argument(
        "--grid",
        action="store_true",
        help="Shorthand for --strategy grid.",
    )
    parser.add_argument(
        "--cell-km",
        type=float,
        default=DEFAULT_CELL_KM,
        help="Grid cell size in km. grid/full-harvest only.",
    )
    parser.add_argument(
        "--queries",
        default=None,
        help=(
            'Comma-separated Pass 2 query variants, e.g. "Plumber,Drain '
            'cleaning". full-harvest only; overrides the industry defaults.'
        ),
    )
    parser.add_argument(
        "--target-new-exportable",
        type=int,
        default=None,
        help=(
            f"Per-zip target for newly exportable contacts "
            f"(default {DEFAULT_TARGET_NEW_EXPORTABLE}). Single-centroid only."
        ),
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help=(
            f"Maximum scraper depth before stopping each zip "
            f"(default {DEFAULT_MAX_DEPTH}). Single-centroid only."
        ),
    )
    parser.add_argument(
        "--stale-iterations",
        type=int,
        default=None,
        help=(
            f"Stop zip after this many consecutive zero-progress iterations "
            f"(default {DEFAULT_STALE_ITERATIONS}). Single-centroid only."
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
        "--scraper-concurrency",
        type=int,
        default=None,
        help="Override scraper -c concurrency for this run.",
    )
    parser.add_argument(
        "--scraper-browser-pool-size",
        type=int,
        default=None,
        help="Override scraper -browser-pool-size for this run.",
    )
    parser.add_argument(
        "--scraper-pages-per-browser",
        type=int,
        default=None,
        help="Override scraper -pages-per-browser for this run.",
    )
    parser.add_argument(
        "--scraper-proxy-limit",
        type=int,
        default=None,
        help="Limit forwarded scraper proxies to first N validated entries.",
    )
    parser.add_argument(
        "--scraper-disable-page-reuse",
        action="store_true",
        help="Pass upstream -disable-page-reuse for this run.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help=(
            "Only include contacts with verifier score >= N in the _verified "
            "CSV. The _deduped export and export_history are not gated. NOTE: "
            "this runner has no --verify flag, so unless a previous run "
            "verified these contacts they all score 0 and any N > 0 yields an "
            "empty _verified file. Verify separately with "
            "'python -m app.pipeline.verify_emails', then re-run the export."
        ),
    )
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help=(
            "Base path for the three CSVs the batch writes at the end "
            "(<base>_all / _deduped / _verified — see export_run_outputs). "
            "Defaults to 'data/leads_<query>_<date>.csv' covering the whole "
            "batch (no single location, since a batch spans many rows)."
        ),
    )
    parser.add_argument(
        "--saleshandy",
        action="store_true",
        help="Sort database and export/push 12 Saleshandy campaign permutations at the end of the batch.",
    )
    return parser


def parse_args() -> argparse.Namespace:
    return _build_parser().parse_args()


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """Resolve the strategy and reject flag combinations that would be ignored.

    Same split as run_pipeline.py: a non-positive bound is an error (a `0`
    target is met before the first scrape, a `0` depth can't run an iteration),
    and a flag that the chosen strategy ignores is a warning — grid and
    full-harvest run a fixed set of passes, so nothing there loops on depth.
    """
    strategy = _resolve_strategy(args)

    depth_loop_flags = (
        ("--target-new-exportable", args.target_new_exportable),
        ("--max-depth", args.max_depth),
        ("--stale-iterations", args.stale_iterations),
    )
    for flag, value in depth_loop_flags:
        if value is not None and value <= 0:
            parser.error(f"{flag} must be > 0 (got {value}).")
    if args.cell_km <= 0:
        parser.error(f"--cell-km must be > 0 (got {args.cell_km}).")

    if strategy == "single-centroid":
        if args.cell_km != DEFAULT_CELL_KM:
            logger.warning(
                "--cell-km=%.2f supplied but --strategy is single-centroid, "
                "which has no grid; ignored.",
                args.cell_km,
            )
    else:
        for flag, value in depth_loop_flags:
            if value is not None:
                logger.warning(
                    "%s=%d supplied but --strategy is %s, which does not loop "
                    "on depth; ignored.",
                    flag, value, strategy,
                )

    return strategy


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    setup_logging()

    strategy = _validate_args(args, parser)
    query_variants = _resolve_query_variants(args, strategy, parser)

    disable_scraper_proxy = args.no_proxy or args.no_scraper_proxy
    disable_crawler_proxy = args.no_proxy or args.no_crawler_proxy
    init_db()

    locations = load_locations(args.zip_file)
    logger.info(
        "Loaded %d locations from %s (strategy=%s)",
        len(locations), args.zip_file, strategy,
    )
    if strategy == "full-harvest":
        logger.warning(
            "full-harvest runs a grid pass AND a multi-query centroid sweep "
            "for each of the %d rows — budget accordingly. PASS 3 (fast ZIP "
            "top-up) is skipped: this batch already is the ZIP sweep.",
            len(locations),
        )

    # Shared by all three strategies; every one of these is a per-run knob, not
    # a per-location one.
    common = dict(
        disable_scraper_proxy=disable_scraper_proxy,
        disable_crawler_proxy=disable_crawler_proxy,
        scraper_concurrency=args.scraper_concurrency,
        scraper_browser_pool_size=args.scraper_browser_pool_size,
        scraper_pages_per_browser=args.scraper_pages_per_browser,
        scraper_proxy_limit=args.scraper_proxy_limit,
        scraper_disable_page_reuse=args.scraper_disable_page_reuse,
    )

    rows_attempted = 0
    for location in locations:
        # Paced per row rather than per scrape: single-centroid already paces
        # its own depth loop, so this only spaces out row boundaries.
        if rows_attempted:
            pace(f"batch row {location!r}")
        rows_attempted += 1
        try:
            if strategy == "single-centroid":
                # Geocodes internally, so the centroid still costs exactly one
                # Nominatim call per row — same as the branch below.
                metrics = run_location_pipeline(
                    query=args.query,
                    location=location,
                    max_depth=(
                        DEFAULT_MAX_DEPTH if args.max_depth is None
                        else args.max_depth
                    ),
                    target_new_exportable=(
                        DEFAULT_TARGET_NEW_EXPORTABLE
                        if args.target_new_exportable is None
                        else args.target_new_exportable
                    ),
                    stale_iterations_limit=(
                        DEFAULT_STALE_ITERATIONS if args.stale_iterations is None
                        else args.stale_iterations
                    ),
                    **common,
                )
            else:
                # grid and full-harvest need a bounding box, which a centroid
                # alone doesn't give — resolve geo here and hand it down. A row
                # Nominatim can't box raises inside the strategy and is caught
                # below, so one unmappable ZIP doesn't end the batch.
                lat, lon, bbox = geocode_location(location)
                if strategy == "grid":
                    metrics = run_location_grid(
                        query=args.query,
                        location=location,
                        bbox=bbox,
                        cell_km=args.cell_km,
                        **common,
                    )
                else:
                    metrics = run_location_full_harvest(
                        query=args.query,
                        location=location,
                        bbox=bbox,
                        lat=lat,
                        lon=lon,
                        cell_km=args.cell_km,
                        queries=query_variants,
                        # zip_csv deliberately omitted — see the warning above.
                        **common,
                    )
        except Exception:
            logger.exception("Location run failed for %s. Continuing batch.", location)
            continue

        logger.info(
            "Finished %r: strategy=%s depths=%s new_exportable=%d total_contacts=%d",
            location,
            strategy,
            metrics.depths_run,
            metrics.new_exportable_contacts,
            metrics.total_contacts,
        )

    export_run_outputs(
        min_score=args.min_score,
        csv_path=args.csv_path or _default_csv_path(args.query),
    )

    if args.saleshandy:
        logger.info("--- Sorting and Exporting 12 Saleshandy Campaign Permutations ---")
        try:
            from app.pipeline.export_saleshandy import export_12_saleshandy_permutations, push_to_saleshandy_api
            export_12_saleshandy_permutations(min_score=args.min_score)
            push_to_saleshandy_api(min_score=args.min_score)
        except Exception as se:
            logger.warning("Saleshandy campaign export/push failed: %s", se)


if __name__ == "__main__":
    main()
