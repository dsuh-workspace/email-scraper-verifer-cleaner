#!/usr/bin/env python
"""Export contacts belonging to one run cohort, scoped by business provenance.

Why this exists: `export_new_leads()` has no run-cohort filter — it emits every
contact absent from `export_history` for the destination. On a DB that carries a
baseline cohort, that is the *whole DB*, not the new work. That is exactly how
`data/archive/MISLABELED_wholedb_export_2026-08-04_*.csv` ended up 76/166 San
Jose rows when it was supposed to be a Sunnyvale/Santa Clara HVAC cohort.

This script scopes on `businesses.first_scrape_run_id >= <cohort_start>`, which
is the authoritative net-new signal, and does not touch `export_history` — so it
is repeatable and side-effect free.

Contacts are selected by their *business's* provenance, not their own. Before
the 2026-08-04 fix, crawl-discovered contacts had NULL `first_scrape_run_id`
(only `process_leads.py` stamped it), so filtering on the contact's own column
drops precisely the crawled emails you want.

City routing (`--city`) resolves a business to a city by its own address first,
falling back to the city in `scrape_runs.location` of the run that discovered it.
The fallback matters: crawl-discovered businesses frequently have a blank
`address` (45 of 106 emailed cohort contacts on the 2026-08-04 runs), and
filtering on address alone silently drops them.

Usage:
    export_cohort.py <db> <cohort_start_id> <out.csv> [--min-score N]
                     [--require-email] [--exclude-domain SUBSTR ...]
                     [--city NAME ...] [--drop-junk]

Example:
    export_cohort.py database/test_hvac_overlap.db 50 \\
        data/leads_sunnyvale_santaclara_hvac_2026-08-05_cohort.csv --require-email
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Reuse the pipeline's own junk lists so --drop-junk cannot drift from what the
# crawler filters at write time. Rows already in the DB predate those filters.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.pipeline.extract_emails import (  # noqa: E402
    EXCLUDE_DOMAINS,
    EXCLUDE_EXTENSIONS,
    EXCLUDE_LOCALPARTS,
)

HEADER = [
    "Export Date",
    "Contact Name",
    "Email",
    "Phone",
    "Job Title",
    "Business Name",
    "Website",
    "Category",
    "Review Count",
    "Review Rating",
    "Address",
    "Status",
    "Description",
    "Place ID",
]

QUERY = """
WITH latest_verification AS (
    SELECT contact_id, MAX(id) AS latest_id
    FROM email_verifications
    GROUP BY contact_id
)
SELECT
    c.name, c.email, c.phone, c.title,
    b.business_name, b.website, b.category, b.review_count, b.review_rating,
    b.address, b.status, b.description, b.place_id,
    ev.score,
    sr.location
FROM contacts c
JOIN businesses b ON b.id = c.business_id
LEFT JOIN latest_verification lv ON lv.contact_id = c.id
LEFT JOIN email_verifications ev ON ev.id = lv.latest_id
LEFT JOIN scrape_runs sr ON sr.id = b.first_scrape_run_id
WHERE b.first_scrape_run_id >= ?
ORDER BY b.business_name, c.email
"""

# "123 Main St, Santa Clara, CA 95050, United States" -> "Santa Clara"
_ADDR_CITY = re.compile(r",\s*([^,]+?),\s*[A-Z]{2}\b")
# "Santa Clara, CA 95050" -> "Santa Clara"
_RUN_CITY = re.compile(r"^\s*([^,]+?)\s*,")


def resolve_city(address: str | None, run_location: str | None) -> str:
    """Best-known city for a business.

    The business's own address wins — that is where it actually is, and
    ZIP-centroid scraping legitimately returns businesses from adjacent cities.
    Only when the address is blank do we fall back to the discovering run's
    location, which is the ZIP that was searched rather than the business's home.
    """
    match = _ADDR_CITY.search(address or "")
    if match:
        return match.group(1).strip()
    match = _RUN_CITY.match(run_location or "")
    return match.group(1).strip() if match else ""


def is_junk(email: str) -> bool:
    """Apply the crawler's own blocklists to rows written before it had them."""
    low = email.lower()
    if low.endswith(EXCLUDE_EXTENSIONS):
        return True
    if low.partition("@")[0] in EXCLUDE_LOCALPARTS:
        return True
    return any(bad in low for bad in EXCLUDE_DOMAINS)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("db")
    ap.add_argument("cohort_start_id", type=int)
    ap.add_argument("out_csv")
    ap.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Keep only contacts whose latest verification score >= N "
        "(Reacher map: safe=95, risky=50, unknown=25, invalid=10). "
        "Unverified contacts are dropped when this is set.",
    )
    ap.add_argument(
        "--require-email",
        action="store_true",
        help="Drop blank-email contacts. Off by default to match the pipeline's "
        "deliberate behavior (CLAUDE.md intentional deferral #12).",
    )
    ap.add_argument(
        "--exclude-domain",
        action="append",
        default=[],
        metavar="SUBSTR",
        help="Drop contacts whose email contains SUBSTR. Repeatable. Use for the "
        "off-domain crawl contamination seen in prior runs (e.g. --exclude-domain .gov).",
    )
    ap.add_argument(
        "--city",
        action="append",
        default=[],
        metavar="NAME",
        help="Keep only businesses resolving to NAME (case-insensitive). Repeatable. "
        "Resolved from the business address, falling back to the discovering run's "
        "location when the address is blank.",
    )
    ap.add_argument(
        "--drop-junk",
        action="store_true",
        help="Drop emails matching the crawler's own EXCLUDE_EXTENSIONS / "
        "EXCLUDE_DOMAINS lists. Rows written before those filters existed still "
        "carry asset filenames (logo@2x.png) and web-agency relay addresses.",
    )
    args = ap.parse_args()

    wanted_cities = {c.strip().lower() for c in args.city}

    conn = sqlite3.connect(args.db)
    rows = conn.execute(QUERY, (args.cohort_start_id,)).fetchall()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kept = 0
    dropped = {
        "blank_email": 0, "min_score": 0, "excluded_domain": 0,
        "junk": 0, "other_city": 0,
    }
    spillover: dict[str, int] = {}

    with open(args.out_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for r in rows:
            (
                name, email, phone, title,
                biz_name, website, category, review_count, review_rating,
                address, status, description, place_id,
                score, run_location,
            ) = r

            email_text = (email or "").strip()
            if args.require_email and not email_text:
                dropped["blank_email"] += 1
                continue
            if args.min_score is not None and (score is None or score < args.min_score):
                dropped["min_score"] += 1
                continue
            if any(sub.lower() in email_text.lower() for sub in args.exclude_domain):
                dropped["excluded_domain"] += 1
                continue
            if args.drop_junk and email_text and is_junk(email_text):
                dropped["junk"] += 1
                continue
            if wanted_cities:
                city = resolve_city(address, run_location)
                if city.lower() not in wanted_cities:
                    dropped["other_city"] += 1
                    spillover[city or "(unknown)"] = spillover.get(city or "(unknown)", 0) + 1
                    continue

            writer.writerow([
                stamp, name, email_text, phone, title,
                biz_name, website, category, review_count, review_rating,
                address, status, description, place_id,
            ])
            kept += 1

    print(f"Cohort         : businesses.first_scrape_run_id >= {args.cohort_start_id}")
    print(f"Candidate rows : {len(rows)}")
    print(f"Written        : {kept} -> {args.out_csv}")
    for reason, n in dropped.items():
        if n:
            print(f"  dropped ({reason}): {n}")
    if spillover:
        # Named explicitly so a city filter never silently discards real leads.
        print("  cities excluded by --city:")
        for city, n in sorted(spillover.items(), key=lambda x: -x[1]):
            print(f"    {n:4d}  {city}")
    if not kept:
        print("  WARNING: nothing written — check the cohort start id.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
