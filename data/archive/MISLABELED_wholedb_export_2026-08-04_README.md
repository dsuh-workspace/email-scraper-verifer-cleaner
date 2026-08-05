# Quarantined: `MISLABELED_wholedb_export_2026-08-04_*.csv`

**Do not use for outreach. Do not cite as overlap-test output.**

These two files were originally named `data/hvac_overlap_test_all.csv` and
`data/hvac_overlap_test_deduped.csv`, which is wrong on both counts — they are
neither HVAC-only nor an overlap-test cohort.

They are a **whole-DB export** from `database/test_hvac_overlap.db`, produced by
`export_new_leads()` on 2026-08-04 00:06. Because that DB was seeded from a copy
of the mixed main DB, and because export is gated on `export_history` rather than
on a run cohort, the export swept up the entire pre-existing San Jose baseline
alongside the candidate-market rows.

Composition of the `_deduped` file (166 rows):

| Address city | Rows |
|---|---|
| San Jose | 76 |
| other / blank | 47 |
| Santa Clara | 33 |
| Sunnyvale | 10 |

The top rows are San Jose **plumbing** companies (Plumbtree Plumbing 95119,
Elite Rooter, Fluid Dynamics Plumbing) — i.e. the vertical is wrong too.

Only 43 of 166 rows are actually Santa Clara/Sunnyvale.

## What to do instead

Re-export from `database/test_hvac_overlap.db` filtered to the candidate cohort
(`first_scrape_run_id >= 50`) once the crawl is finished. See the
"Continuation commands" section of `RUNS.md`.

## Root cause

`export_new_leads()` has no run-cohort filter — it exports every contact absent
from `export_history` for the destination. On a DB that carries a baseline, that
is the whole DB, not the new work. Seed candidate DBs from a **single-vertical**
baseline and export with an explicit cohort filter.
