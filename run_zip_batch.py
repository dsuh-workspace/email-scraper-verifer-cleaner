"""Batch zip-code runner with per-zip new-exportable gating."""

import argparse
import csv
from pathlib import Path

from app.db.create_tables import init_db
from app.logging_config import get_logger, setup_logging
from app.pipeline.export_sheets import export_new_leads
from run_pipeline import run_location_pipeline

logger = get_logger(__name__)



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



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run batch lead generation from CSV of zip codes/locations."
    )
    parser.add_argument("--query", required=True, help="Industry keyword to scrape")
    parser.add_argument("--zip-file", required=True, help="CSV file of zip/location rows")
    parser.add_argument(
        "--target-new-exportable",
        type=int,
        default=20,
        help="Per-zip target for newly exportable contacts",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=20,
        help="Maximum scraper depth before stopping each zip",
    )
    parser.add_argument(
        "--stale-iterations",
        type=int,
        default=2,
        help="Stop zip after this many consecutive zero-progress iterations",
    )
    return parser.parse_args()



def main() -> None:
    args = parse_args()
    setup_logging()
    init_db()

    locations = load_locations(args.zip_file)
    logger.info("Loaded %d locations from %s", len(locations), args.zip_file)

    for location in locations:
        try:
            metrics = run_location_pipeline(
                query=args.query,
                location=location,
                max_depth=args.max_depth,
                target_new_exportable=args.target_new_exportable,
                stale_iterations_limit=args.stale_iterations,
            )
        except Exception:
            logger.exception("Location run failed for %s. Continuing batch.", location)
            continue

        logger.info(
            "Finished %r: depths=%s new_exportable=%d total_contacts=%d",
            location,
            metrics.depths_run,
            metrics.new_exportable_contacts,
            metrics.total_contacts,
        )

    export_new_leads()


if __name__ == "__main__":
    main()
