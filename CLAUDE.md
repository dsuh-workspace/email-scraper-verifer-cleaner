# email-scraper-verifer-cleaner — Operator Notes

Current operator guide for the HVAC/Plumbing lead-gen pipeline. Keep this
file focused on present-tense behavior, open work, and runbook details.
Shipped history and closed review items live in `CHANGELOG.md`.

Purpose: scrape Google Maps → ingest into SQLite → dedupe → crawl sites for
emails → optionally verify via local Reacher → export to Sheets/CSV.
The pipeline also captures rich map details (review count/rating, address,
status, description, place ID).

## Current strategy and CLI contract

- Three strategies are supported on both `run_pipeline.py` and
  `run_zip_batch.py`: `single-centroid`, `grid`, and `full-harvest`.
  `--grid` is shorthand for `--strategy grid`.
- `grid` requires Playwright via `./scripts/setup_scraper_playwright.sh`.
- `full-harvest` runs grid pass 1, per-variant slow centroid pass 2, and
  optional ZIP top-up pass 3.
- Pass 2 defaults to **per-variant subprocesses** (`--pass2-per-variant`)
  to avoid the vendored scraper's combined-query undercount. The old
  combined behavior is opt-in via `--pass2-combined` for diagnostics only.
- Default harvest query sets are intentionally pruned: HVAC defaults to 3
  variants and plumbing defaults to 2 based on recent San Jose reruns.
- One vertical per run. A query that names both HVAC and plumbing does not
  silently sweep both; full-harvest exits 2 unless explicit `--queries`
  are supplied.
- `--queries` is valid only with `full-harvest`; other strategies exit 2.
- `--min-contacts` / `--max-depth` are single-centroid only. Under
  `grid`/`full-harvest` they warn and are ignored; non-positive values
  exit 2.
- CSV fallback filenames are descriptive by default:
  `data/leads_<location>_<query>_<date>.csv` for single-location runs and
  `data/leads_<query>_<date>.csv` for batch runs. `--csv-path` overrides.

## Pipeline flow

Three strategies via `--strategy {single-centroid, grid, full-harvest}`
on `run_pipeline.py` and `run_zip_batch.py`. Default = `single-centroid`.
Step-by-step flow lives in the code — see `run_location_pipeline()`,
`run_location_grid()`, `run_location_full_harvest()` in `run_pipeline.py`
(full pseudocode moved to `CHANGELOG.md` if needed for reference).

- **Single-centroid**: depth loop (step +2) up to `--max-depth`; each
  iteration scrapes → dedupes → crawls, stopping early once
  `target_new_exportable` new exportable contacts are reached.
  `run_zip_batch.py` adds `--stale-iterations` on top of the same loop.
- **Grid**: one bbox-based scrape (`cell_km`, depth=3), then a single
  dedupe/crawl/export pass.
- **Full-harvest**: grid Pass 1 → per-variant slow-centroid Pass 2
  (depth=10, `fast_mode=False`) → optional fast ZIP top-up Pass 3
  (depth=3, `fast_mode=True`) → one shared dedupe/crawl/export.
- All strategies share the same tail: `--verify` runs
  `verify_contacts_emails()`, then `export_new_leads(min_score=min_score)`.

## Strategy entrypoints

Each strategy is one function in `run_pipeline.py`, and both CLIs call the
same three:

| Function | Strategy | Returns |
|---|---|---|
| `run_location_pipeline()` | single-centroid | `LocationRunMetrics` |
| `run_location_grid()` | grid | `LocationRunMetrics` |
| `run_location_full_harvest()` | full-harvest | `LocationRunMetrics` |

`run_end_to_end_pipeline` is geocode + dispatch + verify/export tail.
Grid and full-harvest raise when Nominatim returns no bounding box rather
than silently degrading to centroid mode.

### `run_zip_batch.py` flags

- `--strategy` / `--grid` / `--cell-km` / `--queries` match
  `run_pipeline.py` spelling and validation.
- `_resolve_strategy` and `_resolve_query_variants` are imported from
  `run_pipeline`, not reimplemented.
- `--target-new-exportable` / `--max-depth` / `--stale-iterations`
  default to `None` and are single-centroid only. Effective defaults:
  20 / `DEFAULT_MAX_DEPTH` / 2.
- `--cell-km` warns under single-centroid and must be `> 0`.
- Geocoding: single-centroid geocodes inside `run_location_pipeline`;
  grid/full-harvest geocode in the batch loop to get each row's bbox.
- Batch full-harvest never passes `zip_csv` because the batch already is
  the ZIP sweep.
- A row that fails (unmappable ZIP, scraper error) is logged and skipped;
  the batch continues, and export still runs once at the end.

Verification (`app/pipeline/verify_emails.py`) is wired into
`run_pipeline.py` and is the supported path. Opt in with `--verify`.
Export can be gated by `--min-score N` — score map is in `README.md`
("Verification"). Verifier failures warn but are not fatal.

## Crawl-attempt ledger

Column semantics, env tuning, and the force-re-crawl SQL live in
`README.md` under "Crawl-attempt notes". Two details it doesn't cover:

- Pending set in `extract_emails.py`: already has email → done;
  `crawl_attempts >= max_attempts` → given up; `last_crawled_at` inside
  the cooldown → skip; else crawl.
- `CRAWL_RETRY_AFTER_HOURS=0` and non-integer values are invalid and fall
  back to the 720-hour default rather than erroring.

## Run tracking

**Check `RUNS.md` before starting a new city/vertical run.** Update it
after any real production run completes.

For manual SQL evaluation of incremental yield and market overlap between runs, see `RUNBOOK_SQL_OVERLAP_ANALYSIS.md`.

## Open work

1. **Short-circuit the `mock` SPREADSHEET_ID** so Sheets auth is not
   attempted before CSV fallback.
2. **Use provenance fields for future lift tables**: rely on
   `businesses.first_scrape_run_id` / `contacts.first_scrape_run_id`
   instead of raw-lead first-seen inference. **Two caveats apply to data
   written before 2026-08-04:** legacy rows have NULL provenance and were
   never backfilled, and crawl-created contacts were never stamped at all.
   For historical cohorts, scope contacts by their *business's* provenance
   and fall back to `MIN(raw_leads.scrape_run_id)` for NULL businesses.
   `scripts/analysis/market_overlap.py` does both.
3. **Backfill NULL `first_scrape_run_id`** on legacy `businesses` / `contacts`
   rows from `MIN(raw_leads.scrape_run_id)`, so cohort queries stop needing the
   inference fallback. Not done — queries are in `MAINTENANCE_SQL.md`.
4. **Give `export_new_leads()` an optional run-cohort filter.** It currently
   emits every contact absent from `export_history`, which on a DB carrying a
   baseline is the whole DB. `scripts/analysis/export_cohort.py` works around
   this but the export path itself is still unscoped.
5. **`tests/test_run_scraper.py` proxy-order tests are flaky** (5 failures,
   pre-existing). They assert a fixed proxy order against code that shuffles
   randomly. Seed the shuffle or assert set-equality.

## Analysis tooling

Cohort/lift analysis lives in `scripts/analysis/` and runs inside `.venv`
(stdlib + SQLAlchemy only — **pandas is not a dependency**, so the retired
root-level `calculate_overlap.py` / `get_runtimes.py` could not run there):

| Script | Purpose |
|---|---|
| `market_overlap.py` | Business/contact overlap and lift for a run cohort. Reuses the pipeline's own dedupe keys; handles NULL provenance and cross-vertical contamination. |
| `export_cohort.py` | Cohort-scoped CSV export. Side-effect free — does not touch `export_history`. |
| `run_wallclock.py` | Active wall-clock time for a cohort, merging overlapping run intervals. |

`market_overlap.py` replaces `calculate_overlap.py`, which matched raw leads to
businesses on `place_id` — a key the pipeline never uses for dedupe.

## Market-overlap setup rules

San Jose ↔ Sunnyvale/Santa Clara overlap came in at **10.8% (plumbing)** and
**12.6% (HVAC)** — the adjacent market is ~87–89% net-new. Numbers, caveats,
and the continuation runbook are in `RUNS.md`; methodology is in
`RUNBOOK_SQL_OVERLAP_ANALYSIS.md`.

Two rules before starting any market test, both learned the hard way:

- **Seed the candidate DB from a single-vertical baseline.** A shared baseline
  puts same-market/different-vertical runs below the cohort cutoff, where they
  get miscounted as overlap (this inflated HVAC 12.6% → 19.6%).
- **One pipeline process per DB.** Three concurrent HVAC processes interleaved
  run IDs and made the wall-clock figure incomparable to a sequential run.

## Intentional deferrals

- **Export pushes empty-email rows to Sheets** — deliberate project
  decision. Blank-email contacts may still export.
- **Commits inside per-business loop** — batching every 25 is
  acceptable for now.
- **No `robots.txt` / no per-domain politeness** — per-host locking is
  in place; `robots.txt` is still ignored.

## Environment / operational

### Python / venv conventions

Setup is in `README.md` ("Python version / virtualenv"). Run every Python
command — tests and CLI entrypoints included — inside `.venv`.

### Local Reacher instance

URL, start script, and score map are in `README.md` ("Verification").
Operator details it doesn't cover:

- `start_local_verifier.sh` no-ops if the endpoint is already reachable
  and restarts an existing stopped `reacher-backend` container when it
  can, so re-running it is safe.
- `./scripts/stop_local_verifier.sh` stops and removes the container.
- No auth on the endpoint itself.
- Apple Silicon runs the published Docker image under emulation.

### Scraper auto-update

- `scripts/update_scraper.sh` pulls `../google-maps-scraper`, rebuilds,
  smoke-tests, and only swaps in the new binary if output still matches
  what `run_scraper.py` expects.
- Weekly launchd job: `com.apl.update-scraper` (Sunday 03:17).
- Tracked plist: `scripts/com.apl.update-scraper.plist`.
- Check status with `launchctl print gui/$(id -u)/com.apl.update-scraper`.
- Launchd logs to `logs/update_scraper.launchd.log`; script logs to
  `logs/update_scraper.log`.

### DB migration notes

`init_db()` runs `_apply_additive_columns()` after `create_all()` to add
missing additive columns, currently:

- `businesses.last_crawled_at`
- `businesses.crawl_attempts`
- `businesses.first_scrape_run_id`
- `contacts.first_scrape_run_id`
- `export_history.exported_at`

This is idempotent and additive-only. Backfills, non-additive changes, and
legacy-DB catch-up live in `MAINTENANCE_SQL.md`.

Provenance stamping, as currently implemented:

- `process_and_deduplicate_leads()` copies `RawLead.scrape_run_id` onto new
  `Business` / `Contact` rows as `first_scrape_run_id`.
- `harvest_emails_from_websites()` stamps crawl-discovered contacts using
  `MAX(scrape_runs.id)` at harvest start. The crawl is not itself a scrape
  run, so its contacts are attributed to the cohort whose pipeline
  invocation produced them.

Rows written before 2026-08-04 predate the crawl-path stamping and carry
NULL provenance — see Open work #3/#4 for how to work around it and
`MAINTENANCE_SQL.md` for the backfill.

## Settled decisions

- Sheets export stays. `SPREADSHEET_ID=mock` should fall back to CSV; the
  remaining bug is the auth short-circuit.
- Category is not hardcoded. Scraper-reported categories win; query is the
  fallback.
- `min_contacts` means new exportable contacts produced by this run, not
  cumulative DB contacts.
- One vertical per run: HVAC and plumbing are not combined implicitly.

## Notes

- `max_depth=20` is mostly legacy compatibility for single-centroid.
- Prefer `new_exportable_contacts` over `total_contacts` when evaluating
  batch runs.
- `new exportable` does not mean verified or even non-blank-email-only.
- Broad ZIP sweeps can still surface junk emails; spot-check exports
  before outreach or verification.

See `CHANGELOG.md` for shipped history and closed review items.
