"""Run experiment cluster A (query variants) sequentially."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scrape_experiment import scrape

SJ_GEO = "37.3361663,-121.8905910"  # San Jose centroid, Nominatim
SJ_BBOX = "37.1231596,-122.0462270,37.4691477,-121.5858438"  # full SJ bbox

CLUSTER = sys.argv[1] if len(sys.argv) > 1 else "A"


def cluster_A():
    """Query text variants — single geo, depth=3, fast-mode."""
    variants = [
        ("A1_plumbing", "Plumbing"),
        ("A2_plumber", "Plumber"),
        ("A3_plumbing_svcs", "Plumbing services"),
        ("A4_emergency", "Emergency plumber"),
        ("A5_24hr", "24 hour plumber"),
        ("A6_drain", "Drain cleaning"),
        ("A7_water_heater", "Water heater repair"),
        ("A8_leak", "Leak repair"),
    ]
    for label, q in variants:
        scrape(label, [q], geo=SJ_GEO, depth=3, timeout_sec=600)


def cluster_As():
    """Query variants — slow mode, depth=10 (max coverage per query)."""
    variants = [
        ("As1_plumbing", "Plumbing"),
        ("As2_plumber", "Plumber"),
        ("As3_plumbing_svcs", "Plumbing services"),
        ("As4_emergency", "Emergency plumber"),
        ("As5_drain", "Drain cleaning"),
        ("As6_water_heater", "Water heater repair"),
        ("As7_leak", "Leak repair"),
        ("As8_sewer", "Sewer service"),
    ]
    for label, q in variants:
        scrape(label, [q], geo=SJ_GEO, depth=10, fast_mode=False, timeout_sec=1800)


def cluster_Am():
    """Multi-query in one input file — see if scraper dedups internally."""
    q = ["Plumbing", "Plumber", "Plumbing services", "Emergency plumber",
         "Drain cleaning", "Water heater repair", "Leak repair", "Sewer service"]
    scrape("Am_multi_slow_d10", q, geo=SJ_GEO, depth=10, fast_mode=False, timeout_sec=3600)


def cluster_B():
    """Depth scaling — 'Plumbing' at various depths. Fast + non-fast."""
    for label, d in [("B1_fast_d1", 1), ("B2_fast_d3", 3), ("B3_fast_d10", 10)]:
        scrape(label, ["Plumbing"], geo=SJ_GEO, depth=d, fast_mode=True, timeout_sec=900)
    for label, d in [("B4_slow_d1", 1), ("B5_slow_d3", 3), ("B6_slow_d10", 10), ("B7_slow_d20", 20)]:
        scrape(label, ["Plumbing"], geo=SJ_GEO, depth=d, fast_mode=False, timeout_sec=1800)


def cluster_C():
    """Zoom levels — 'Plumbing' depth=3."""
    for label, z in [("C1_z12", 12), ("C2_z14", 14), ("C3_z15", 15), ("C4_z17", 17)]:
        scrape(label, ["Plumbing"], geo=SJ_GEO, depth=3, zoom=z, timeout_sec=600)


def cluster_D():
    """Grid over SJ bbox. depth=3 keeps per-cell time ~40s.
    5km ≈ 60 cells, 3km ≈ 170 cells."""
    scrape("D1_grid_5km_d3", ["Plumbing"], grid_bbox=SJ_BBOX, grid_cell_km=5.0,
           depth=3, fast_mode=False, timeout_sec=7200)
    scrape("D2_grid_3km_d3", ["Plumbing"], grid_bbox=SJ_BBOX, grid_cell_km=3.0,
           depth=3, fast_mode=False, timeout_sec=14400)


def cluster_Dt():
    """Tight bbox (~populated SJ core) at 3km cell."""
    tight_bbox = "37.20,-121.99,37.44,-121.75"  # rough SJ core, ~27km × 21km
    scrape("Dt1_tight_3km_d3", ["Plumbing"], grid_bbox=tight_bbox, grid_cell_km=3.0,
           depth=3, fast_mode=False, timeout_sec=7200)


def cluster_Dm():
    """Grid + multi-query in same input file. If scraper iterates cells × queries."""
    tight_bbox = "37.20,-121.99,37.44,-121.75"
    q = ["Plumbing", "Plumber", "Drain cleaning", "Water heater repair",
         "Emergency plumber", "Leak repair"]
    scrape("Dm1_grid_multi", q, grid_bbox=tight_bbox, grid_cell_km=3.0,
           depth=3, fast_mode=False, timeout_sec=14400)


def cluster_Dr():
    """Repeat Dt1 to check nondeterminism / additional coverage."""
    tight_bbox = "37.20,-121.99,37.44,-121.75"
    scrape("Dr1_repeat", ["Plumbing"], grid_bbox=tight_bbox, grid_cell_km=3.0,
           depth=3, fast_mode=False, timeout_sec=7200)


def _sweep(prefix: str, *, fast: bool, depth: int, timeout: int) -> None:
    import csv, requests, time
    csv_path = Path(__file__).resolve().parents[1] / "san_jose_zips.csv"
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        zip_code = row["zip"]
        label = f"{prefix}_zip_{zip_code}"
        q = f"{zip_code}, {row['city']}, {row['state']}"
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1},
                headers={"User-Agent": "exp/1.0"}, timeout=10,
            )
            d = r.json()
            if not d:
                print(f"[{label}] geocode failed"); continue
            geo = f"{d[0]['lat']},{d[0]['lon']}"
        except Exception as e:
            print(f"[{label}] geocode err: {e}"); continue
        scrape(label, ["Plumbing"], geo=geo, depth=depth,
               fast_mode=fast, timeout_sec=timeout)
        time.sleep(1.1)  # nominatim rate courtesy


def cluster_E():
    """ZIP sweep FAST mode. 28 zips × ~2s = ~1min scraping (~30s geo)."""
    _sweep("Ef", fast=True, depth=3, timeout=300)


def cluster_Es():
    """ZIP sweep SLOW mode d=10. 28 zips × ~100s = ~47min."""
    _sweep("Es", fast=False, depth=10, timeout=1800)


CLUSTERS = {"A": cluster_A, "As": cluster_As, "Am": cluster_Am,
            "B": cluster_B, "C": cluster_C, "D": cluster_D, "Dt": cluster_Dt,
            "Dm": cluster_Dm, "Dr": cluster_Dr,
            "E": cluster_E, "Es": cluster_Es}

if __name__ == "__main__":
    fn = CLUSTERS.get(CLUSTER)
    if not fn:
        print(f"Unknown cluster: {CLUSTER}. Choose {list(CLUSTERS)}"); sys.exit(1)
    fn()
