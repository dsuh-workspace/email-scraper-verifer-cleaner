"""One-off grid experiment: iterate lat/lon centroids over a city bbox,
invoke the scraper binary per centroid in fast-mode, aggregate unique
businesses.

Bypasses the Python pipeline's DB layer — outputs raw JSON per tile,
dedupes across tiles by place_id (falls back to name+address).

Usage:
    .venv/bin/python scripts/grid_experiment.py \
        --query "Plumbing in San Jose, CA" \
        --bbox 37.21,-122.05,37.47,-121.75 \
        --cell-km 2.0 \
        --depth 1 \
        --concurrency 6 \
        --out /tmp/grid_sj_results.json
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "app" / "scraper" / "google-maps-scraper"
KM_PER_DEG_LAT = 111.32


def build_proxy_arg() -> str:
    proxy_file = os.getenv("SCRAPER_PROXIES_FILE", "proxies.txt")
    if not proxy_file or not Path(proxy_file).exists():
        return ""
    lines = []
    for line in Path(proxy_file).read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return ",".join(lines)


def generate_centroids(bbox: tuple[float, float, float, float], cell_km: float):
    min_lat, min_lon, max_lat, max_lon = bbox
    lat_step = cell_km / KM_PER_DEG_LAT
    mid_lat = (min_lat + max_lat) / 2.0
    lon_step = cell_km / (KM_PER_DEG_LAT * max(math.cos(math.radians(mid_lat)), 1e-6))

    lat_cells = max(1, int(math.ceil((max_lat - min_lat) / lat_step)))
    lon_cells = max(1, int(math.ceil((max_lon - min_lon) / lon_step)))

    centroids = []
    for i in range(lat_cells):
        lat = min_lat + (i + 0.5) * lat_step
        if lat > max_lat:
            break
        for j in range(lon_cells):
            lon = min_lon + (j + 0.5) * lon_step
            if lon > max_lon:
                break
            centroids.append((round(lat, 6), round(lon, 6)))
    return centroids


def scrape_tile(query: str, lat: float, lon: float, depth: int, proxies: str,
                index: int, total: int) -> list[dict]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as qf:
        qf.write(query + "\n")
        qpath = qf.name
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as rf:
        rpath = rf.name
    try:
        cmd = [
            str(BINARY),
            "-input", qpath,
            "-results", rpath,
            "-json",
            "-geo", f"{lat},{lon}",
            "-depth", str(depth),
            "-fast-mode",
            "-c", "1",
            "-exit-on-inactivity", "2m",
        ]
        if proxies:
            cmd += ["-proxies", proxies]
        started = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            print(f"[{index+1}/{total}] tile {lat},{lon}: FAILED rc={result.returncode} in {elapsed:.1f}s",
                  file=sys.stderr, flush=True)
            print(result.stderr[-500:], file=sys.stderr, flush=True)
            return []
        records = []
        try:
            content = Path(rpath).read_text().strip()
            if not content:
                pass
            elif content.startswith("["):
                records = json.loads(content)
            else:
                # JSONL fallback
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"[{index+1}/{total}] tile {lat},{lon}: JSON parse error: {e}",
                  file=sys.stderr, flush=True)
            return []
        print(f"[{index+1}/{total}] tile {lat},{lon}: {len(records)} biz in {elapsed:.1f}s",
              file=sys.stderr, flush=True)
        return records
    finally:
        for p in (qpath, rpath):
            try:
                os.remove(p)
            except OSError:
                pass


def dedup_key(rec: dict) -> str:
    for key in ("place_id", "placeId", "cid"):
        v = rec.get(key)
        if v:
            return f"{key}:{v}"
    name = (rec.get("title") or rec.get("name") or "").strip().lower()
    addr = (rec.get("address") or "").strip().lower()
    return f"nameaddr:{name}|{addr}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--query", required=True)
    p.add_argument("--bbox", required=True, help="minLat,minLon,maxLat,maxLon")
    p.add_argument("--cell-km", type=float, default=2.0)
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--out", required=True, help="Path for aggregated deduplicated JSON")
    args = p.parse_args()

    if not BINARY.exists():
        print(f"scraper binary not found at {BINARY}", file=sys.stderr)
        return 1

    parts = [float(x) for x in args.bbox.split(",")]
    if len(parts) != 4:
        print("bbox must be 4 comma-separated floats", file=sys.stderr)
        return 1
    bbox = tuple(parts)  # type: ignore
    centroids = generate_centroids(bbox, args.cell_km)
    total = len(centroids)
    print(f"Grid: {total} cells at {args.cell_km} km", file=sys.stderr)

    proxies = build_proxy_arg()
    print(f"Proxies: {len(proxies.split(',')) if proxies else 0}", file=sys.stderr)

    all_records: dict[str, dict] = {}
    counts_per_tile: list[int] = []
    started = time.monotonic()

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {
            ex.submit(scrape_tile, args.query, lat, lon, args.depth, proxies, i, total): (i, lat, lon)
            for i, (lat, lon) in enumerate(centroids)
        }
        for fut in cf.as_completed(futures):
            i, lat, lon = futures[fut]
            try:
                records = fut.result()
            except Exception as e:
                print(f"tile {lat},{lon} raised: {e}", file=sys.stderr, flush=True)
                records = []
            valid = [r for r in records if isinstance(r, dict)]
            counts_per_tile.append(len(valid))
            for r in valid:
                k = dedup_key(r)
                if k not in all_records:
                    all_records[k] = r

    elapsed = time.monotonic() - started

    Path(args.out).write_text(json.dumps(list(all_records.values()), indent=2))

    print("", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Total tiles run: {total}", file=sys.stderr)
    print(f"Total raw records: {sum(counts_per_tile)}", file=sys.stderr)
    print(f"Unique businesses (deduped): {len(all_records)}", file=sys.stderr)
    print(f"Average per tile: {sum(counts_per_tile)/max(1,len(counts_per_tile)):.1f}", file=sys.stderr)
    print(f"Empty tiles: {sum(1 for c in counts_per_tile if c == 0)}", file=sys.stderr)
    print(f"Elapsed: {elapsed/60:.1f} min", file=sys.stderr)
    print(f"Output: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
