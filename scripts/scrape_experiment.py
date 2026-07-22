"""
Scrape-only experiment harness.

Invokes the google-maps-scraper binary directly for a labeled experiment,
writing raw JSON to experiments/<label>.json. No DB, no crawl.

Skip fast-mode when using -grid-bbox (incompatible per gosom/google-maps-scraper).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BINARY = REPO_ROOT / "app" / "scraper" / "google-maps-scraper"
EXPERIMENTS_DIR = REPO_ROOT / "experiments"


def scrape(
    label: str,
    queries: list[str],
    *,
    geo: str | None = None,
    grid_bbox: str | None = None,
    grid_cell_km: float | None = None,
    depth: int = 3,
    zoom: int | None = None,
    lang: str = "en",
    email: bool = False,
    fast_mode: bool = True,
    timeout_sec: int = 1200,
    concurrency: int = 1,
    out_dir: Path | str | None = None,
) -> dict:
    """Run scraper once. Returns metadata dict with stats and out path.

    out_dir defaults to experiments/. Production callers pass their own dir.
    """
    target_dir = Path(out_dir) if out_dir else EXPERIMENTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{label}.json"
    log_path = target_dir / f"{label}.log"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as qf:
        qf.write("\n".join(queries) + "\n")
        query_file = qf.name

    cmd = [
        str(BINARY),
        "-input", query_file,
        "-results", str(out_path),
        "-json",
        "-depth", str(depth),
        "-pages-per-browser", "2",
        "-c", str(concurrency),
        "-lang", lang,
    ]
    if fast_mode and not grid_bbox:
        cmd.append("-fast-mode")
    if email:
        cmd.append("-email")
    if geo:
        cmd.extend(["-geo", geo])
    if grid_bbox:
        cmd.extend(["-grid-bbox", grid_bbox])
        if grid_cell_km is not None:
            cmd.extend(["-grid-cell", str(grid_cell_km)])
    if zoom is not None:
        cmd.extend(["-zoom", str(zoom)])

    env = os.environ.copy()
    # Strip any proxy env before subprocess call.
    for k in (
        "SCRAPER_PROXIES", "SCRAPER_PROXIES_FILE",
        "CRAWLER_PROXY", "CRAWLER_PROXY_FILE",
        "CRAWLER_HTTP_PROXY", "CRAWLER_HTTPS_PROXY",
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    ):
        env.pop(k, None)

    print(f"[{label}] cmd: {' '.join(cmd)}")
    t0 = time.time()
    try:
        with open(log_path, "w") as lf:
            proc = subprocess.run(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=timeout_sec,
                check=False,
            )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    dt = time.time() - t0
    os.unlink(query_file)

    # Parse output JSON (line-delimited or array).
    leads: list[dict] = []
    if out_path.exists() and out_path.stat().st_size > 0:
        try:
            with open(out_path, "r", encoding="utf-8") as rf:
                first = rf.read(1)
                rf.seek(0)
                if first == "[":
                    leads = json.load(rf)
                else:
                    # NDJSON fallback
                    for line in rf:
                        line = line.strip()
                        if line:
                            leads.append(json.loads(line))
        except Exception as e:
            print(f"[{label}] parse err: {e}")

    meta = {
        "label": label,
        "queries": queries,
        "geo": geo,
        "grid_bbox": grid_bbox,
        "grid_cell_km": grid_cell_km,
        "depth": depth,
        "zoom": zoom,
        "fast_mode": fast_mode and not grid_bbox,
        "email": email,
        "concurrency": concurrency,
        "rc": rc,
        "wall_sec": round(dt, 1),
        "n_leads": len(leads),
        "out": str(out_path),
        "log": str(log_path),
    }
    meta_path = target_dir / f"{label}.meta.json"
    with open(meta_path, "w") as mf:
        json.dump(meta, mf, indent=2)
    print(f"[{label}] rc={rc} n={len(leads)} wall={dt:.1f}s")
    return meta


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True)
    p.add_argument("--query", required=True, help="single or comma-separated queries")
    p.add_argument("--geo", default=None)
    p.add_argument("--grid-bbox", default=None)
    p.add_argument("--grid-cell", type=float, default=None)
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--zoom", type=int, default=None)
    p.add_argument("--lang", default="en")
    p.add_argument("--email", action="store_true")
    p.add_argument("--no-fast", action="store_true")
    p.add_argument("--timeout", type=int, default=1200)
    p.add_argument("--concurrency", type=int, default=1)
    args = p.parse_args()

    queries = [q.strip() for q in args.query.split(",") if q.strip()]
    scrape(
        label=args.label,
        queries=queries,
        geo=args.geo,
        grid_bbox=args.grid_bbox,
        grid_cell_km=args.grid_cell,
        depth=args.depth,
        zoom=args.zoom,
        lang=args.lang,
        email=args.email,
        fast_mode=not args.no_fast,
        timeout_sec=args.timeout,
        concurrency=args.concurrency,
    )


if __name__ == "__main__":
    main()
