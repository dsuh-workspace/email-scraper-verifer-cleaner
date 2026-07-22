"""Comprehensive analysis of all experiments.

Loads every experiments/*.json + meta, computes:
- Per-experiment stats
- Cluster union coverage
- Recommended combos w/ time vs unique tradeoff
- Businesses with website (email-harvestable subset)
"""
from __future__ import annotations
import glob
import json
import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXP = REPO / "experiments"


def _key(L):
    did = (L.get("data_id") or "").strip()
    return did or f"{(L.get('title') or '').strip().lower()}||{(L.get('address') or '').strip().lower()}"


def load(pattern: str) -> dict[str, tuple[dict, list[dict]]]:
    out = {}
    for meta_p in sorted((EXP).glob(f"{pattern}.meta.json")):
        label = meta_p.stem[:-5]
        json_p = EXP / f"{label}.json"
        if not json_p.exists() or json_p.stat().st_size == 0:
            continue
        try:
            with open(meta_p) as f:
                meta = json.load(f)
            leads = []
            with open(json_p, "r", encoding="utf-8") as f:
                first = f.read(1); f.seek(0)
                if first == "[":
                    leads = json.load(f)
                else:
                    for line in f:
                        line = line.strip()
                        if line:
                            leads.append(json.loads(line))
            out[label] = (meta, leads)
        except Exception as e:
            print(f"[{label}] load err: {e}")
    return out


def _union(data: dict, keys_only=False) -> set[str]:
    s = set()
    for _lbl, (_m, leads) in data.items():
        s |= {_key(L) for L in leads}
    return s


def _with_web(data: dict) -> set[str]:
    s = set()
    for _lbl, (_m, leads) in data.items():
        for L in leads:
            if L.get("web_site"):
                s.add(_key(L))
    return s


def _total_time(data: dict) -> float:
    return sum((m.get("wall_sec") or 0) for m, _ in data.values())


def main():
    strategies = {
        "A_fast_variants (8 fast queries centroid)": load("A[0-9]_*") | load("A[1-8]*"),
        "As_slow_variants (8 slow queries centroid)": load("As*"),
        "Am_multi_slow (multi-query one call centroid)": load("Am_*"),
        "Dt1_grid_3km_tight (grid + 1 query)": load("Dt1_*"),
        "Dm1_grid_multi (grid + 6 queries)": load("Dm1_*"),
        "E_fast_zip (28 ZIPs fast centroid)": load("E_zip_*"),
        "Es_slow_zip (28 ZIPs slow centroid)": load("Es_zip_*"),
    }

    # Filter empty
    strategies = {k: v for k, v in strategies.items() if v}

    print(f"\n{'STRATEGY':<50}  {'wall(s)':>10}  {'unique':>7}  {'w/web':>7}  {'per_min':>8}")
    print("-" * 100)
    sets = {}
    web_sets = {}
    times = {}
    for name, data in strategies.items():
        u = _union(data)
        w = _with_web(data)
        t = _total_time(data)
        sets[name] = u
        web_sets[name] = w
        times[name] = t
        per_min = len(u) / (t / 60) if t else 0
        print(f"{name:<50}  {t:>10.1f}  {len(u):>7}  {len(w):>7}  {per_min:>8.1f}")

    if not sets:
        print("no experiments loaded")
        return

    # Overall union
    all_u = set()
    for s in sets.values():
        all_u |= s
    all_w = set()
    for s in web_sets.values():
        all_w |= s
    print(f"\nGRAND UNION: {len(all_u)} businesses ({len(all_w)} with website)")

    # Best combos (per unique per time)
    print("\nRecommended combos (time-sorted):")
    combos = [
        ("Am only", ["Am_multi_slow (multi-query one call centroid)"]),
        ("Grid only", ["Dt1_grid_3km_tight (grid + 1 query)"]),
        ("Grid + Am", ["Dt1_grid_3km_tight (grid + 1 query)", "Am_multi_slow (multi-query one call centroid)"]),
        ("Grid + Am + fast ZIP", ["Dt1_grid_3km_tight (grid + 1 query)", "Am_multi_slow (multi-query one call centroid)", "E_fast_zip (28 ZIPs fast centroid)"]),
        ("Grid-multi only", ["Dm1_grid_multi (grid + 6 queries)"]),
        ("ALL", list(sets.keys())),
    ]
    print(f"\n  {'combo':<40} {'wall':>8}  {'unique':>7}  {'w/web':>7}")
    print("  " + "-" * 70)
    for name, keys in combos:
        keys_ok = [k for k in keys if k in sets]
        if not keys_ok:
            continue
        u = set()
        w = set()
        t = 0
        for k in keys_ok:
            u |= sets[k]; w |= web_sets[k]; t += times[k]
        print(f"  {name:<40} {t/60:>7.1f}m  {len(u):>7}  {len(w):>7}")


if __name__ == "__main__":
    main()
