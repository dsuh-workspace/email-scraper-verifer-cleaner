#!/usr/bin/env python
"""Measure business/contact overlap and lift for a candidate run cohort.

Replaces the old root-level `calculate_overlap.py`, which matched raw leads to
businesses on `place_id`. The pipeline never uses `place_id` for dedupe
(`process_leads.py` keys on base domain, then business_name + E164 phone), so
place_id matching can group raw leads differently from how the pipeline actually
deduped them. This script imports the pipeline's own `extract_domain` and
`normalize_phone` so matching is identical by construction.

Two other correctness details this handles and the SQL runbook does not:

1. **NULL provenance.** Businesses created before `first_scrape_run_id` existed
   have NULL there. A plain `first_scrape_run_id < cutoff` filter silently drops
   them from the overlap bucket. Here, NULL falls back to the minimum
   `scrape_run_id` across all matching `raw_leads` rows.

2. **Cross-vertical contamination.** If the candidate DB was seeded from a
   multi-vertical baseline, runs from the *same* candidate market but a
   *different* vertical can sit below the cohort cutoff and be miscounted as
   baseline overlap. Pass `--cohort NAME=IDS` labels to see the attribution
   split and correct for it.

Usage:
    market_overlap.py <db> <candidate_start_id> [--cohort NAME=1-31,33] ...

Example:
    market_overlap.py database/test_hvac_overlap.db 50 \\
        --cohort SJ-plumbing=1-31 \\
        --cohort SJ-HVAC=32,34,41-49 \\
        --cohort SCSun-plumbing=33,35-40
"""

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.pipeline.process_leads import extract_domain, normalize_phone  # noqa: E402

# Mirrors the local set inside process_and_deduplicate_leads().
_BAD_DOMAINS = {"http:", "https:", ""}


def parse_id_spec(spec: str) -> set[int]:
    """'1-31,33,35-40' -> {1..31, 33, 35..40}"""
    ids: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            ids.update(range(int(lo), int(hi) + 1))
        else:
            ids.add(int(part))
    return ids


def build_business_index(conn):
    """Replicate the pipeline's dedupe keys: domain, then (name, E164 phone)."""
    biz, by_domain, by_name_phone = {}, {}, {}
    for b in conn.execute(
        "SELECT id, business_name, domain, phone, address, first_scrape_run_id FROM businesses"
    ):
        biz[b["id"]] = b
        if b["domain"] and b["domain"] not in _BAD_DOMAINS:
            by_domain[b["domain"]] = b["id"]
        if b["business_name"] and b["phone"]:
            by_name_phone[(b["business_name"], b["phone"])] = b["id"]
    return biz, by_domain, by_name_phone


def match_raw_lead(row, by_domain, by_name_phone):
    """Domain first (strongest signal), then exact name + normalized phone."""
    if row["website"]:
        dom = extract_domain(row["website"].strip())
        if dom and dom not in _BAD_DOMAINS and dom in by_domain:
            return by_domain[dom]
    if row["business_name"] and row["phone"]:
        phone = normalize_phone(row["phone"])
        if phone:
            return by_name_phone.get((row["business_name"], phone))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="Path to the SQLite DB")
    ap.add_argument("candidate_start_id", type=int, help="First scrape_runs.id of the candidate cohort")
    ap.add_argument(
        "--cohort",
        action="append",
        default=[],
        metavar="NAME=IDS",
        help="Label a baseline cohort for overlap attribution, e.g. SJ-HVAC=32,34,41-49",
    )
    args = ap.parse_args()

    cutoff = args.candidate_start_id
    labels: dict[str, set[int]] = {}
    for entry in args.cohort:
        if "=" not in entry:
            ap.error(f"--cohort needs NAME=IDS, got {entry!r}")
        name, spec = entry.split("=", 1)
        labels[name] = parse_id_spec(spec)

    def label_for(run_id):
        if run_id is None:
            return "unknown (no provenance)"
        for name, ids in labels.items():
            if run_id in ids:
                return name
        return f"run {run_id}"

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    runs = {r["id"]: r for r in conn.execute("SELECT id, location, category, status FROM scrape_runs")}
    cand_run_ids = sorted(i for i in runs if i >= cutoff)
    if not cand_run_ids:
        print(f"No scrape_runs with id >= {cutoff}.")
        return 1

    biz, by_domain, by_name_phone = build_business_index(conn)

    # Infer first-seen per business from ALL raw_leads, for NULL-provenance rows.
    inferred_first: dict[int, int] = {}
    for row in conn.execute("SELECT scrape_run_id, business_name, website, phone FROM raw_leads"):
        bid = match_raw_lead(row, by_domain, by_name_phone)
        if bid is None:
            continue
        run_id = row["scrape_run_id"]
        if bid not in inferred_first or run_id < inferred_first[bid]:
            inferred_first[bid] = run_id

    def first_seen(bid):
        """Stamped provenance wins; fall back to inferred for legacy NULL rows."""
        stamped = biz[bid]["first_scrape_run_id"]
        return stamped if stamped is not None else inferred_first.get(bid)

    # Which businesses did the candidate runs touch?
    touched: dict[int, set[int]] = {}
    raw_rows = unmatched = 0
    for row in conn.execute(
        "SELECT scrape_run_id, business_name, website, phone FROM raw_leads WHERE scrape_run_id >= ?",
        (cutoff,),
    ):
        raw_rows += 1
        bid = match_raw_lead(row, by_domain, by_name_phone)
        if bid is None:
            unmatched += 1
            continue
        touched.setdefault(bid, set()).add(row["scrape_run_id"])

    net_new = {b for b in touched if (fs := first_seen(b)) is not None and fs >= cutoff}
    overlap = set(touched) - net_new

    # Authoritative net-new count: stamped provenance, independent of re-matching.
    stamped_new = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM businesses WHERE first_scrape_run_id >= ?", (cutoff,)
        )
    }

    contacts = defaultdict(list)
    for c in conn.execute("SELECT id, business_id, email, first_scrape_run_id FROM contacts"):
        contacts[c["business_id"]].append(c)

    def has_email(c):
        return c["email"] is not None and c["email"].strip() != ""

    def count_contacts(bids):
        total = exportable = 0
        for b in bids:
            for c in contacts.get(b, []):
                total += 1
                exportable += bool(has_email(c))
        return total, exportable

    print(f"\n{'=' * 74}")
    print(f"DB       : {args.db}")
    print(f"Candidate: runs >= {cutoff}  ({len(cand_run_ids)} runs)")
    print(f"{'=' * 74}")

    print(f"\ncandidate raw_lead rows           : {raw_rows}")
    print(f"  matched to a business          : {raw_rows - unmatched}")
    print(f"  unmatched (no domain/phone key): {unmatched}")

    # Headline denominator = every business the candidate cohort produced or
    # re-encountered. Stamped-new rows are authoritative even when re-matching
    # misses them (no domain and no name+phone key), so union them in rather
    # than dividing by the matched-only subset, which understates the base.
    all_new = stamped_new | net_new
    all_found = all_new | overlap
    print(f"\nbusinesses found by candidate     : {len(all_found)}")
    print(f"  NET NEW                        : {len(all_new)}")
    print(f"  OVERLAP (pre-existing)         : {len(overlap)}")
    if all_found:
        print(f"  --> overlap rate               : {100 * len(overlap) / len(all_found):.1f}%")

    total_touched = len(touched)
    print(f"\n(diagnostic) re-matched from raw_leads: {total_touched} touched, {len(net_new)} new")
    if len(stamped_new) > len(net_new):
        print(
            f"  {len(stamped_new) - len(net_new)} stamped-new businesses were not re-matched "
            "from candidate\n  raw_leads — they lack both a domain and a name+phone key."
        )

    nn_tot, nn_exp = count_contacts(all_new)
    ov_tot, ov_exp = count_contacts(overlap)
    print(f"\ncontacts on touched businesses    : {nn_tot + ov_tot} ({nn_exp + ov_exp} exportable)")
    print(f"  on NET NEW businesses          : {nn_tot} ({nn_exp} exportable)")
    print(f"  on OVERLAP businesses          : {ov_tot} ({ov_exp} exportable)")

    stamped_contacts = [
        c
        for b in touched
        for c in contacts.get(b, [])
        if c["first_scrape_run_id"] is not None and c["first_scrape_run_id"] >= cutoff
    ]
    null_prov = sum(
        1 for b in all_new for c in contacts.get(b, []) if c["first_scrape_run_id"] is None
    )
    print(
        f"\ncontacts stamped into cohort      : {len(stamped_contacts)} "
        f"({sum(1 for c in stamped_contacts if has_email(c))} exportable)"
    )
    if null_prov:
        print(
            f"  !! {null_prov} contacts on net-new businesses have NULL provenance.\n"
            "     Pre-fix crawl data (extract_emails.py did not stamp first_scrape_run_id).\n"
            "     Use the 'on NET NEW businesses' figure above, not the stamped count."
        )

    if overlap:
        print("\n--- overlap attribution: which cohort first found them ---")
        attrib = Counter(label_for(first_seen(b)) for b in overlap)
        for name, n in attrib.most_common():
            print(f"  {name:<26} {n:>4}  ({100 * n / len(overlap):.1f}% of overlap)")
        foreign = [n for name, n in attrib.items() if "SCSun" in name or "candidate" in name.lower()]
        if foreign:
            true_overlap = len(overlap) - sum(foreign)
            print(
                f"\n  Corrected: {sum(foreign)} of these were first found by candidate-market runs\n"
                f"  sitting below the cutoff (different vertical, same market). True cross-market\n"
                f"  overlap = {true_overlap}/{len(all_found)} = {100 * true_overlap / len(all_found):.1f}%"
            )

    print("\n--- per candidate location (business counted once per ZIP) ---")
    loc = defaultdict(lambda: [0, 0, 0, 0])
    for bid, run_ids in touched.items():
        for location in {runs[r]["location"] for r in run_ids}:
            loc[location][0] += 1
            if bid in all_new:
                loc[location][1] += 1
                t, e = count_contacts([bid])
                loc[location][2] += t
                loc[location][3] += e
    print(f"{'location':<26}{'touched':>9}{'net new':>9}{'nn contacts':>13}{'nn export':>11}")
    for location in sorted(loc):
        s = loc[location]
        print(f"{location:<26}{s[0]:>9}{s[1]:>9}{s[2]:>13}{s[3]:>11}")

    bad = [i for i in cand_run_ids if runs[i]["status"] != "completed"]
    if bad:
        print(f"\n!! candidate runs not completed: {bad}")
        for i in bad:
            print(f"     run {i}: {runs[i]['location']} / {runs[i]['query'] if 'query' in runs[i].keys() else runs[i]['category']} -> {runs[i]['status']}")
        print("   Coverage is partial; treat lift as a floor, not a final number.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
