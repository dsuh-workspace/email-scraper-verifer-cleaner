"""Best-strategy harvester for a target city.

Runs three complementary Google Maps scrape passes with NO PROXY and
merges into one deduped JSONL. Meant to be run once per (city × industry).

Pass 1 (Grid, ~10 min):    3km cells over a tight city bbox, single query.
                           Discovers most businesses geographically.
Pass 2 (Multi-query, ~7 min): 8 semantic query variants at city centroid.
                           Adds businesses whose search-term-match is different.
Pass 3 (Fast ZIP, ~1 min): fast-mode at every ZIP centroid (if provided).
                           Cheap top-up. Fast-mode returns partly-different pins
                           than slow-mode (only ~42% overlap in SJ tests).

Empirical result for "Plumbing in San Jose, CA":
    Pass 1: 362 unique | Pass 2: 263 unique | Pass 3: 168 unique
    UNION: 504 unique businesses in ~18 min (of which 352 have websites)

Usage (from repo root, with venv activated):
    python scripts/harvest_best.py \\
      --industry "Plumbing" \\
      --bbox "37.20,-121.99,37.44,-121.75" \\
      --centroid "37.336,-121.891" \\
      --zips-csv san_jose_zips.csv \\
      --out data/plumbing_sanjose.jsonl
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scrape_experiment import scrape

REPO = Path(__file__).resolve().parents[1]


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s.strip().lower()).strip("_")
    return s or "run"

DEFAULT_QUERIES = [
    "Plumbing", "Plumber", "Plumbing services", "Emergency plumber",
    "Drain cleaning", "Water heater repair", "Leak repair", "Sewer service",
]


def _key(L):
    did = (L.get("data_id") or "").strip()
    return did or f"{(L.get('title') or '').strip().lower()}||{(L.get('address') or '').strip().lower()}"


def _load(path: Path) -> list[dict]:
    leads = []
    if not path.exists() or path.stat().st_size == 0:
        return leads
    with open(path, "r", encoding="utf-8") as f:
        first = f.read(1)
        f.seek(0)
        if first == "[":
            leads = json.load(f)
        else:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        leads.append(json.loads(line))
                    except Exception:
                        pass
    return leads


def _geocode(query: str) -> str | None:
    import requests
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 1},
        headers={"User-Agent": "harvest-best/1.0"},
        timeout=10,
    )
    d = r.json()
    if not d:
        return None
    return f"{d[0]['lat']},{d[0]['lon']}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--industry", required=True,
                   help="Seed query keyword (e.g. 'Plumbing')")
    p.add_argument("--queries", default=None,
                   help="Comma-separated query variants (default: 8 plumbing terms)")
    p.add_argument("--bbox", required=True,
                   help="minLat,minLon,maxLat,maxLon (tight city bbox)")
    p.add_argument("--centroid", required=True,
                   help="lat,lon of city center for multi-query pass")
    p.add_argument("--cell-km", type=float, default=3.0)
    p.add_argument("--depth-slow", type=int, default=10)
    p.add_argument("--depth-grid", type=int, default=3)
    p.add_argument("--zips-csv", default=None,
                   help="CSV with 'zip,city,state' header for fast ZIP top-up pass")
    p.add_argument("--city-slug", default=None,
                   help="Slug for output dir (e.g. 'san_jose'). Derived from --centroid if unset.")
    p.add_argument("--out", default=None,
                   help="Output JSONL path. Default: data/harvests/<slug>/<date>/leads.jsonl")
    p.add_argument("--work-dir", default=None,
                   help="Per-pass scraper output dir. Default: data/harvests/<slug>/<date>/passes/")
    p.add_argument("--skip-grid", action="store_true")
    p.add_argument("--skip-multi", action="store_true")
    p.add_argument("--skip-zip", action="store_true")
    args = p.parse_args()

    queries = [q.strip() for q in (args.queries.split(",") if args.queries else DEFAULT_QUERIES) if q.strip()]

    # Build output paths under data/harvests/<slug>/<date>/
    slug = args.city_slug or _slug(f"{args.industry}_{args.centroid}")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    harvest_root = REPO / "data" / "harvests" / slug / date_str
    work_dir = Path(args.work_dir) if args.work_dir else harvest_root / "passes"
    out_path = Path(args.out) if args.out else harvest_root / "leads.jsonl"
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Harvest dir: {harvest_root}")

    all_leads: dict[str, dict] = {}
    total_wall = 0.0

    # PASS 1: grid
    if not args.skip_grid:
        print("\n== PASS 1: Grid over city bbox ==")
        m = scrape("pass1_grid", [args.industry],
                   grid_bbox=args.bbox, grid_cell_km=args.cell_km,
                   depth=args.depth_grid, fast_mode=False, timeout_sec=7200,
                   out_dir=work_dir)
        total_wall += m["wall_sec"]
        for L in _load(Path(m["out"])):
            all_leads[_key(L)] = L
        print(f"  → running union: {len(all_leads)}")

    # PASS 2: multi-query at centroid
    if not args.skip_multi:
        print("\n== PASS 2: Multi-query at centroid ==")
        m = scrape("pass2_multi", queries,
                   geo=args.centroid, depth=args.depth_slow,
                   fast_mode=False, timeout_sec=3600,
                   out_dir=work_dir)
        total_wall += m["wall_sec"]
        for L in _load(Path(m["out"])):
            all_leads[_key(L)] = L
        print(f"  → running union: {len(all_leads)}")

    # PASS 3: fast ZIP top-up
    if not args.skip_zip and args.zips_csv:
        print("\n== PASS 3: Fast ZIP top-up ==")
        with open(args.zips_csv) as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            z = row["zip"]
            q = f"{z}, {row['city']}, {row['state']}"
            geo = _geocode(q)
            if not geo:
                print(f"  [zip {z}] geocode failed"); continue
            m = scrape(f"pass3_zip_{z}", [args.industry],
                       geo=geo, depth=3, fast_mode=True, timeout_sec=300,
                       out_dir=work_dir)
            total_wall += m["wall_sec"]
            for L in _load(Path(m["out"])):
                all_leads[_key(L)] = L
            time.sleep(1.1)
        print(f"  → running union: {len(all_leads)}")

    # Write output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for L in all_leads.values():
            f.write(json.dumps(L) + "\n")

    n_web = sum(1 for L in all_leads.values() if L.get("web_site"))
    n_email = sum(1 for L in all_leads.values() if L.get("emails"))
    n_phone = sum(1 for L in all_leads.values() if L.get("phone"))
    print(f"\n== DONE ==")
    print(f"  Total unique businesses: {len(all_leads)}")
    print(f"  With website:  {n_web}")
    print(f"  With email:    {n_email}  (0 expected: -email flag disabled)")
    print(f"  With phone:    {n_phone}")
    print(f"  Total wall time: {total_wall/60:.1f} min")
    print(f"  Output: {out_path}")


if __name__ == "__main__":
    main()
