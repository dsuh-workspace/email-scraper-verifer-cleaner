#!/usr/bin/env python
"""Active wall-clock time for a run cohort, merging overlapping intervals.

Usage:
    run_wallclock.py <db> <cohort_start_id>

Example:
    run_wallclock.py database/test_hvac_overlap.db 50

CAVEAT: this reports elapsed *active* time, not compute cost. Intervals are
unioned, so if N pipeline processes ran concurrently against the same DB the
result is the wall-clock envelope, not the sum of their work. Two cohorts are
only comparable as throughput if both ran single-process. (The 2026-08-04
Sunnyvale/Santa Clara HVAC cohort ran 3 concurrent processes; the plumbing
cohort ran strictly sequentially — their durations are not comparable.)

Runs still in 'running'/'interrupted' state have NULL completed_at and are
excluded, so an interrupted cohort's total is an underestimate.

Stdlib only — pandas is not a declared dependency of this repo, and the
previous root-level `get_runtimes.py` could not run inside the pinned `.venv`.
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta

# Gap below which two runs are treated as one continuous working window.
GRACE = timedelta(minutes=5)


def parse_ts(value: str) -> datetime:
    """SQLite stores '2026-08-04 06:29:12.345678'; tolerate a missing .ffffff."""
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized timestamp: {value!r}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("db")
    ap.add_argument("cohort_start_id", type=int)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    rows = conn.execute(
        "SELECT started_at, completed_at FROM scrape_runs "
        "WHERE id >= ? AND completed_at IS NOT NULL AND started_at IS NOT NULL "
        "ORDER BY started_at",
        (args.cohort_start_id,),
    ).fetchall()

    excluded = conn.execute(
        "SELECT COUNT(*) FROM scrape_runs WHERE id >= ? AND completed_at IS NULL",
        (args.cohort_start_id,),
    ).fetchone()[0]

    if not rows:
        print("No completed runs found in this cohort yet.")
        return 0

    merged: list[list[datetime]] = []
    for started_at, completed_at in rows:
        start, end = parse_ts(started_at), parse_ts(completed_at)
        if merged and start <= merged[-1][1] + GRACE:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    total_min = sum((end - start).total_seconds() / 60 for start, end in merged)

    print(f"Completed runs in cohort      : {len(rows)}")
    print(f"Active wall-clock duration    : {total_min:.1f} minutes ({total_min / 60:.1f} h)")
    if excluded:
        print(f"Excluded (no completed_at)    : {excluded} run(s) — total is an underestimate")
    print(f"Active windows                : {len(merged)}")
    for start, end in merged:
        mins = (end - start).total_seconds() / 60
        print(f"  {start} -> {end}  ({mins:.1f} min)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
