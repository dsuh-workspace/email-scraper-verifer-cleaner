# Run tracker — city × vertical

Source of truth: `database/hvac_leads.db` → `scrape_runs` table (query,
location, category, started_at/completed_at). Check this file before
starting a new city/vertical run; update it after any real production run
completes (not one-off experiments or throwaway DBs).

One vertical per run (see CLAUDE.md "Answered / settled" #4) — don't mix
HVAC and Plumbing in a single row.

| City | Vertical | Date | Strategy | Status | Notes |
|---|---|---|---|---|---|
| San Jose | HVAC | 2026-08-02 | full-harvest, per-variant Pass 2 (lift-table run) | **Done** | Pruned `DEFAULT_HVAC_HARVEST_QUERIES` 8→3 (HVAC, Heating and cooling, HVAC contractor) from this run's yield data. Consolidated output: `data/leads_sanjose_hvac_2026-08-03_final.csv` (16 businesses). |
| San Jose | Plumbing | 2026-08-02 | full-harvest, per-variant Pass 2 | **Done** | Completed on fresh DB `database/hvac_leads.san_jose_plumbing_rerun_2026-08-02c.db`. Outputs: consolidated into `data/leads_sanjose_plumbing_2026-08-03_final.csv` (68 businesses); this run's own `_verified` / `_all` / `_deduped` moved to `data/archive/`. Final run: 68 contacts total, 14 new exportable, **0 website-harvested email contacts — 44/44 crawled domains yielded nothing**, against a ~35% success baseline in the main DB (49 hit / 90 miss). Treat that as a crawl failure, not a real result; the 14 emails here came from map data. Re-crawl needed for real plumbing coverage on this city. Pass 2 was heavily concentrated in `Plumbing` and `Plumber`; the other six variants produced empty/missing output files on this run, so the plumbing lift-table/pruning follow-up remains open. |
| Santa Clarita | HVAC | 2026-08-02 | full-harvest, per-variant Pass 2 | **Done** | Completed on fresh per-city DB `database/hvac_leads.santa_clarita_hvac_2026-08-02_fresh.db` (not the shared main DB). Outputs: `data/leads_santa_clarita_hvac_2026-08-02_final.csv` (28 rows, outreach-ready) and `_verified.csv` (35 rows, pre-cleanup); `_all` / `_deduped` moved to `data/archive/`. Quality caveat: `_verified` includes off-domain crawl emails (`member_services@rioradio.org`, `astigma@astigmatic.com`, `flags@2x.png`) — stripped in `_final`. |
| Santa Clarita | Plumbing | 2026-08-02 | full-harvest, per-variant Pass 2 | **Done** | Completed on fresh per-city DB `database/hvac_leads.santa_clarita_plumbing_2026-08-02_fresh.db` (not the shared main DB). Outputs: `data/leads_santa_clarita_plumbing_2026-08-02_final.csv` (71 rows, outreach-ready) and `_verified.csv` (245 rows, pre-cleanup); `_all` / `_deduped` moved to `data/archive/`. Large contamination in `_verified` from off-domain/support-site emails (e.g. `imtresidential.com`, `newapthome.com`, `santaclarita.gov`, `latofonts.com`) — filtered out in `_final`, which is the file to use for outreach. |
| Santa Clara, CA + Sunnyvale | Plumbing | 2026-08-02 | ZIP top-up (fast mode) | Done | **Different city from Santa Clarita** (South Bay, not LA County) — don't confuse the two when picking the next city to run. |
| Sunnyvale/Santa Clara | HVAC | 2026-08-04 | full-harvest, overlap test | **Incomplete — resume** | Overlap test vs San Jose baseline. DB `database/test_hvac_overlap.db`, cohort start ID **50**. **Only 1 of 7 ZIPs (95050) is variant-complete**; 95051 and 94086 are partial and 95054 / 94085 / 94087 / 94089 were never scraped. 15 runs completed, 4 interrupted (56, 62, 63, 68) when the processes were hard-killed ~09:00 and ~16:21. 212 min active wall-clock, but **3 concurrent pipeline processes** shared this DB, so that figure is not comparable to the plumbing row. Crawl unfinished: 92/172 net-new businesses never crawled. Interim outreach files (a floor, pending crawl completion): `data/leads_santa_clara_hvac_2026-08-04_final.csv` (26 rows), `data/leads_sunnyvale_hvac_2026-08-04_final.csv` (11 rows). See "Continuation" below. |
| Sunnyvale/Santa Clara | Plumbing | 2026-08-04 | full-harvest, overlap test | **Near-complete** | Overlap test vs San Jose baseline. DB `database/test_plumbing_overlap.db`, cohort start ID **10**. 6 of 7 ZIPs variant-complete; **94089 is missing the `Plumber` variant** (run 42 interrupted). 31 runs completed, 2 interrupted (22, 42). 116 min active wall-clock, strictly single-process. Crawl unfinished: 31/74 net-new businesses never crawled. `export_history` is still empty — the interim files below were produced by `export_cohort.py`, which deliberately does not record exports. Interim outreach files (a floor, pending crawl completion): `data/leads_santa_clara_plumbing_2026-08-04_final.csv` (25 rows), `data/leads_sunnyvale_plumbing_2026-08-04_final.csv` (12 rows). See "Continuation" below. |

## Result: San Jose ↔ Sunnyvale/Santa Clara overlap (2026-08-04)

**Sunnyvale/Santa Clara is ~87–89% net-new inventory against the San Jose
baseline. It is worth running as its own market.** Two verticals agree, so the
question is settled; no further *overlap methodology* experiments are needed.

Measured with `scripts/analysis/market_overlap.py`, which reuses the pipeline's
own dedupe keys (base domain, then `business_name` + E164 phone) rather than
`place_id`, and falls back to `raw_leads`-inferred first-seen for legacy rows
whose `first_scrape_run_id` is NULL.

| | Plumbing (7 ZIPs) | HVAC (3 ZIPs touched) |
|---|---|---|
| Businesses found by candidate | 83 | 214 |
| Net-new | 74 | 172 |
| Overlap | 9 | 42 |
| **Overlap rate (raw)** | **10.8%** | **19.6%** |
| **Overlap rate (corrected)** | **10.8%** | **12.6%** |
| Contacts on net-new businesses | 86 (43 with email) | 163 (63 with email) |

Reproduce:

```bash
source .venv/bin/activate
python scripts/analysis/market_overlap.py database/test_plumbing_overlap.db 10 \
    --cohort SJ-plumbing-baseline=1-9
python scripts/analysis/market_overlap.py database/test_hvac_overlap.db 50 \
    --cohort SJ-plumbing=1-31 --cohort SJ-HVAC=32,34,41-49 \
    --cohort SCSun-plumbing=33,35-40
```

**Why HVAC needs a correction.** `test_hvac_overlap.db` is *not* a fresh DB — it
was seeded from a copy of the mixed main DB, so it carries San Jose plumbing
(runs 1–31), San Jose HVAC (32, 34, 41–49), **and** Santa Clara/Sunnyvale
plumbing (33, 35–40). Those last runs are candidate-market work from a different
vertical sitting *below* the cohort cutoff of 50, so 15 of the 42 "overlaps" are
Santa Clara/Sunnyvale businesses being miscounted as San Jose. Verified by
address: 37 Santa Clara + 13 Sunnyvale vs only 6 San Jose. Removing them leaves
27 genuine San Jose overlaps — 21 from SJ HVAC, 6 from SJ plumbing (combined
plumbing+HVAC shops, e.g. Super Brothers).

Plumbing needs no correction: single-vertical baseline, all 9 overlaps trace to
runs 1–9.

**Caveats.** The business-level rate is trustworthy for the ZIPs actually
covered, because it comes from map scraping, which did complete there. The
*contact* figures are a floor — the crawl never finished (see below). HVAC's rate
rests on 3 ZIPs, so treat plumbing's 10.8% as the better-supported number.

## Continuation — finishing the 2026-08-04 overlap runs

Both DBs are consistent (`raw_leads.processed_at` fully populated, no dangling
ingest). The 6 hard-killed rows have been marked `interrupted` rather than left
at `running`, so cohort queries and `run_wallclock.py` no longer count them as
live: HVAC 56, 62, 63, 68 and plumbing 22, 42.

`DATABASE_URL` selects the DB — pass it per-command rather than sourcing `.env`.
**Run one process at a time against a given DB.**

### 1. Finish the missing scrapes

Per-ZIP variant coverage, not just "ZIP touched", is what matters. Required per
ZIP: pass-1 grid + one pass-2 run per variant (HVAC 3, plumbing 2).

```bash
source .venv/bin/activate

# HVAC — 6 ZIPs still owed work (only 95050 is variant-complete)
env DATABASE_URL=sqlite:///database/test_hvac_overlap.db \
  python run_zip_batch.py \
    --query "HVAC" \
    --zip-file zips_hvac_remaining_2026-08-04.csv \
    --strategy full-harvest \
    --cell-km 2.0

# Plumbing — 94089 only, missing the "Plumber" variant
env DATABASE_URL=sqlite:///database/test_plumbing_overlap.db \
  python run_zip_batch.py \
    --query "Plumbing" \
    --zip-file zips_plumbing_remaining_2026-08-04.csv \
    --strategy full-harvest \
    --cell-km 2.0
```

Re-running a partially-covered ZIP is safe: `process_and_deduplicate_leads()`
dedupes on domain and name+phone, so businesses are not duplicated. Only
`raw_leads` accumulates, which is diagnostic-only anyway.

### 2. Finish the crawl

This is the step that gates the commercially meaningful number. 123 net-new
businesses across the two DBs have `crawl_attempts = 0` and have never been
crawled (HVAC 92/172, plumbing 31/74).

```bash
env DATABASE_URL=sqlite:///database/test_hvac_overlap.db \
  python -m app.pipeline.extract_emails

env DATABASE_URL=sqlite:///database/test_plumbing_overlap.db \
  python -m app.pipeline.extract_emails
```

The crawl ledger skips businesses that already have an email, that hit
`CRAWL_MAX_ATTEMPTS`, or that are inside `CRAWL_RETRY_AFTER_HOURS` — so this
picks up the never-crawled set without re-hitting spent domains. To force a full
re-crawl instead: `UPDATE businesses SET last_crawled_at = NULL, crawl_attempts = 0;`

### 3. Export, scoped to the cohort

Do **not** use `export_new_leads()` here. It has no run-cohort filter — it emits
every contact absent from `export_history`, which on a DB carrying a baseline is
the whole DB. That is exactly how
`data/archive/MISLABELED_wholedb_export_2026-08-04_*.csv` ended up 76/166 San
Jose rows. Use the cohort-scoped exporter, which is side-effect free and
repeatable:

This is what produced the four `_final` files, one per city × vertical. Re-run
verbatim after the crawl finishes to refresh them:

```bash
for spec in hvac:database/test_hvac_overlap.db:50 \
            plumbing:database/test_plumbing_overlap.db:10; do
  v=${spec%%:*}; rest=${spec#*:}; db=${rest%%:*}; cut=${rest##*:}
  for city in Sunnyvale "Santa Clara"; do
    slug=$(echo "$city" | tr 'A-Z ' 'a-z_')
    python scripts/analysis/export_cohort.py "$db" "$cut" \
      "data/leads_${slug}_${v}_2026-08-04_final.csv" \
      --require-email --drop-junk --city "$city"
  done
done
```

It scopes on `businesses.first_scrape_run_id`, not the contact's own column —
crawl-discovered contacts created before the 2026-08-04 provenance fix have NULL
there, and filtering on it drops precisely the emails you want.

`--city` resolves a business by its own address, falling back to the discovering
run's ZIP only when the address is blank. That fallback is load-bearing here: 45
of the 106 emailed cohort contacts sit on blank-address businesses, so filtering
on address alone would have silently dropped them.

`--drop-junk` applies the crawler's own `EXCLUDE_EXTENSIONS` /
`EXCLUDE_DOMAINS` / `EXCLUDE_LOCALPARTS` to rows written before those filters
existed — 14 HVAC and 5 plumbing contacts. Reusing the pipeline's lists rather
than a private copy means export cleanup cannot drift from crawl-time filtering.

Expect some legitimate geographic spillover: ZIP-centroid scraping at 95050
picks up adjacent San Jose, Milpitas, Mountain View, and Cupertino businesses.
Those are real leads, not contamination, and `--city` reports them rather than
discarding them silently — 12 HVAC and 1 plumbing contact fall outside both
target cities. Drop `--city` (or pass the other city names) to collect them.

### 4. Optional verification

`run_zip_batch.py` has no `--verify` flag; verification lives on
`run_pipeline.py` or as a standalone module.

```bash
env DATABASE_URL=sqlite:///database/test_hvac_overlap.db \
  python -m app.pipeline.verify_emails
```

Start Reacher first with `./scripts/start_local_verifier.sh`. Then re-export
with `--min-score 50` (risky-and-better) or `--min-score 95` (safe only).

### 5. Re-measure

```bash
python scripts/analysis/market_overlap.py database/test_hvac_overlap.db 50 \
    --cohort SJ-plumbing=1-31 --cohort SJ-HVAC=32,34,41-49 \
    --cohort SCSun-plumbing=33,35-40
python scripts/analysis/run_wallclock.py database/test_hvac_overlap.db 50
```

### Next-market hygiene

Two changes remove the correction step entirely:

1. Seed the candidate DB from a **single-vertical** baseline copy, so no
   cross-vertical rows sit below the cohort cutoff.
2. Run **one process at a time**. The 3 concurrent HVAC processes interleaved run
   IDs and made the wall-clock figure incomparable to a sequential run's.

## Conventions

- Row = one real production run. Prefer the live `database/hvac_leads.db`,
  but if a run is intentionally isolated to a fresh per-city DB for yield or
  concurrency reasons, record it here and name the DB explicitly in Notes.
  Experiments and methodology tests still don't count.
- "Done" means scraped + crawled for emails, not necessarily exported —
  check `export_history` / `--min-score` outcome separately if that
  matters for the task at hand.
- If a run predates a pipeline fix that changes yield (e.g. the Pass 2
  per-variant default), mark it **Stale** with the fix and date, don't
  just delete the row — that's the signal to re-run.
