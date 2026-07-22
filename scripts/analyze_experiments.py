"""Analyze scrape experiments.

Loads all experiments/<label>.json + <label>.meta.json.
Reports per-experiment count, unique businesses across experiments,
overlap matrix, cumulative coverage.

Unique key: data_id when present, else normalized (title, address).
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from collections import defaultdict


REPO = Path(__file__).resolve().parents[1]
EXP = REPO / "experiments"


def _key(lead: dict) -> str:
    """Canonical dedup key."""
    did = (lead.get("data_id") or "").strip()
    if did:
        return did
    t = re.sub(r"\s+", " ", (lead.get("title") or "").strip().lower())
    a = re.sub(r"\s+", " ", (lead.get("address") or "").strip().lower())
    return f"{t}||{a}"


def load(label_prefix: str | None = None) -> dict[str, tuple[dict, list[dict]]]:
    """Returns {label: (meta, leads)}."""
    out = {}
    for meta_p in sorted(EXP.glob("*.meta.json")):
        label = meta_p.stem[:-5]  # strip ".meta"
        if label_prefix and not label.startswith(label_prefix):
            continue
        json_p = EXP / f"{label}.json"
        if not json_p.exists() or json_p.stat().st_size == 0:
            continue
        try:
            with open(meta_p) as f:
                meta = json.load(f)
            leads = []
            with open(json_p, "r", encoding="utf-8") as f:
                first = f.read(1)
                f.seek(0)
                if first == "[":
                    leads = json.load(f)
                else:
                    for line in f:
                        line = line.strip()
                        if line:
                            leads.append(json.loads(line))
        except Exception as e:
            print(f"[{label}] load err: {e}", file=sys.stderr)
            continue
        out[label] = (meta, leads)
    return out


def per_exp_stats(data: dict) -> None:
    print(f"\n{'label':<25} {'n':>6} {'uniq':>6} {'web':>6} {'phone':>6} {'wall_s':>8}  extras")
    print("-" * 90)
    for label, (meta, leads) in sorted(data.items()):
        keys = {_key(L) for L in leads}
        n_web = sum(1 for L in leads if L.get("web_site"))
        n_phone = sum(1 for L in leads if L.get("phone"))
        extras = []
        if meta.get("geo"): extras.append(f"geo={meta['geo'][:8]}..")
        if meta.get("grid_bbox"): extras.append(f"grid={meta['grid_cell_km']}km")
        if meta.get("zoom") is not None: extras.append(f"z={meta['zoom']}")
        if meta.get("depth"): extras.append(f"d={meta['depth']}")
        q = ",".join(meta.get("queries", []))
        print(f"{label:<25} {len(leads):>6} {len(keys):>6} {n_web:>6} {n_phone:>6} {meta['wall_sec']:>8.1f}  q={q!r} {' '.join(extras)}")


def union_stats(data: dict, cluster: str = "") -> None:
    """Total unique across a cluster, and marginal add per exp in sequence."""
    print(f"\nCluster '{cluster}' cumulative unique:")
    seen: set[str] = set()
    for label, (_meta, leads) in sorted(data.items()):
        keys = {_key(L) for L in leads}
        new = keys - seen
        seen |= keys
        print(f"  after {label:<25}  +{len(new):>5} new  (union={len(seen)})")


def overlap_matrix(data: dict) -> None:
    labels = sorted(data.keys())
    if len(labels) < 2:
        return
    sets = {L: {_key(x) for x in data[L][1]} for L in labels}
    print(f"\nOverlap matrix (|A∩B| / min(|A|,|B|)):")
    print(" " * 22 + " ".join(f"{L:>16}" for L in labels))
    for a in labels:
        row = [f"{a:<20}  "]
        for b in labels:
            if a == b:
                row.append(f"{'—':>16}")
            else:
                inter = len(sets[a] & sets[b])
                mn = min(len(sets[a]), len(sets[b])) or 1
                pct = inter / mn * 100
                row.append(f"{inter:>3}/{mn:<3} ({pct:>4.0f}%)")
        print(" ".join(row))


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else None
    data = load(prefix)
    if not data:
        print("no experiments loaded (filter=%r)" % prefix); return
    per_exp_stats(data)
    union_stats(data, cluster=prefix or "ALL")
    overlap_matrix(data)


if __name__ == "__main__":
    main()
