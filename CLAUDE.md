# email-scraper-verifer-cleaner — Operator Notes

Current operator guide for the HVAC/Plumbing lead-gen pipeline. Keep this
file focused on present-tense behavior, open work, and decisions — if a
fact belongs in one of the docs below, put it there and link.

Purpose: scrape Google Maps → ingest into SQLite → dedupe → crawl sites for
emails → optionally verify via local Reacher → export to Sheets/CSV.
The pipeline also captures rich map details (review count/rating, address,
status, description, place ID).

| Doc | Holds |
|---|---|
| `README.md` | Setup, env vars, CLI usage, proxies, verification, schema |
| `CHANGELOG.md` | Dated shipped history and closed review items |
| `RUNS.md` | Run tracker (city × vertical) and in-flight run continuations |
| `RUNBOOK_SQL_OVERLAP_ANALYSIS.md` | Cohort/lift/overlap analysis queries |
| `MAINTENANCE_SQL.md` | Hand-run backfills, legacy schema catch-up, hygiene |

## Strategies

Three strategies via `--strategy {single-centroid, grid, full-harvest}` on
both `run_pipeline.py` and `run_zip_batch.py`. Default = `single-centroid`;
`--grid` is shorthand for `--strategy grid`. Each strategy is one function
in `run_pipeline.py` and both CLIs call the same three:

| Function | Strategy | Flow |
|---|---|---|
| `run_location_pipeline()` | single-centroid | Depth loop (step +2) up to `--max-depth`; each iteration scrapes → dedupes → crawls, stopping early once `target_new_exportable` new exportable contacts are reached |
| `run_location_grid()` | grid | One bbox-based scrape (`cell_km`, depth=3), then a single dedupe/crawl pass |
| `run_location_full_harvest()` | full-harvest | Grid Pass 1 → per-variant slow-centroid Pass 2 (depth=10, `fast_mode=False`) → optional fast ZIP top-up Pass 3 (depth=3, `fast_mode=True`) → one shared dedupe/crawl |

All three return `LocationRunMetrics` and share the same tail: `--verify`
runs `verify_contacts_emails()`, then
`export_new_leads(min_score=min_score)`. `run_end_to_end_pipeline` is
geocode + dispatch + that tail.

### Behavior notes

- `grid` requires Playwright via `./scripts/setup_scraper_playwright.sh`.
- Grid and full-harvest raise when Nominatim returns no bounding box rather
  than silently degrading to centroid mode.
- Pass 2 defaults to **per-variant subprocesses** (`--pass2-per-variant`)
  to avoid the vendored scraper's combined-query undercount. The old
  combined behavior is opt-in via `--pass2-combined`, for diagnostics only.
- Default harvest query sets are intentionally pruned: HVAC defaults to 3
  variants and plumbing defaults to 2, based on recent San Jose reruns.

### CLI validation

- One vertical per run. A query that names both HVAC and plumbing does not
  silently sweep both; full-harvest exits 2 unless explicit `--queries`
  are supplied.
- `--queries` is valid only with `full-harvest`; other strategies exit 2.
- `--min-contacts` / `--max-depth` are single-centroid only. Under
  `grid`/`full-harvest` they warn and are ignored; non-positive values
  exit 2.
- `--cell-km` is the mirror image: grid/full-harvest only, warns and is
  ignored under single-centroid. Non-positive values exit 2 on both CLIs,
  checked before strategy dispatch so `--cell-km 0` errors even where the
  flag would otherwise just be ignored.
- CSV fallback filenames are descriptive by default:
  `data/leads_<location>_<query>_<date>.csv` for single-location runs and
  `data/leads_<query>_<date>.csv` for batch runs. `--csv-path` overrides.

### `run_zip_batch.py` deltas

- `--strategy` / `--grid` / `--cell-km` / `--queries` match
  `run_pipeline.py` spelling and validation. `_resolve_strategy` and
  `_resolve_query_variants` are imported from `run_pipeline`, not
  reimplemented.
- `--target-new-exportable` / `--max-depth` / `--stale-iterations` default
  to `None` and are single-centroid only. Effective defaults: 20 /
  `DEFAULT_MAX_DEPTH` / 2. `--stale-iterations` layers onto the same depth
  loop.
- `--cell-km` validation is now shared with `run_pipeline.py`, and
  `DEFAULT_CELL_KM` is imported from it rather than redefined.
- Geocoding: single-centroid geocodes inside `run_location_pipeline`;
  grid/full-harvest geocode in the batch loop to get each row's bbox.
- Batch full-harvest never passes `zip_csv` because the batch already is
  the ZIP sweep.
- A row that fails (unmappable ZIP, scraper error) is logged and skipped;
  the batch continues, and export still runs once at the end.

### Verification and export tail

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

## Block detection, proxy cooldown, pacing

Env vars and the operator-facing rules are in `README.md` ("Block detection
and proxy cooldown", "Sticky proxy assignment", "Pacing between
invocations"). Design points that doc doesn't carry:

- Neither this wrapper nor upstream `gosom/google-maps-scraper` detects
  blocks. A soft-blocked run returns few/zero leads and used to be recorded
  as a normal completion. `app/scraper/block_detect.py` infers it from
  yield instead — there is no HTTP/CAPTCHA signal to read.
- New `scrape_runs.status` value: `blocked`. Nothing gates on status, so
  adding it was safe, but the yield-history baseline counts only
  `completed` runs — otherwise two blocked runs in a row make the second
  look normal and the detector goes quiet exactly when it matters.
- A flag is a signal, not a gate: leads from a flagged run are still
  ingested, and `_assess_run_health()` swallows its own errors so a broken
  history query can never lose them.
- Ledger lives in `data/proxy_health.json` (gitignored), not the DB —
  mutable local state an operator may want to reset by hand. Proxies are
  keyed `user@host:port`; passwords are never written.
- A healthy run **decays one strike** rather than resetting to zero. A
  reset lets a proxy alternate block/success forever without ever retiring.
- Full-pool exhaustion **waits** out the shortest cooldown (up to
  `PROXY_WAIT_MAX_SEC`) and retries before raising `ProxyPoolExhausted`.
  With `--scraper-proxy-limit 3` on a 3-proxy pool, one flagged run parks
  everything; failing hard there would kill every remaining row of a ZIP
  batch. Tests set `PROXY_WAIT_MAX_SEC=0` so they never sleep.
- Sticky assignment uses `hashlib.blake2b`, not `hash()` — the builtin is
  salted per process, so it would pick a different proxy for the same
  query on every invocation. Verified stable across separate processes.
- Pacing (`SCRAPER_PACING_SEC`) is off unless set, applies *between*
  invocations only (depth iterations, Pass 2 variants, Pass 3 ZIPs, batch
  rows) and never before the first one. Invalid values warn and disable.

## Run tracking

**Check `RUNS.md` before starting a new city/vertical run.** Update it
after any real production run completes.

For manual SQL evaluation of incremental yield and market overlap between runs, see `RUNBOOK_SQL_OVERLAP_ANALYSIS.md`.

## Open work

1. **`mock` SPREADSHEET_ID ordering (cosmetic).** `export_sheets.py:47`
   already short-circuits before any credential load, so no auth is
   attempted — but it sits *after* the `CREDENTIALS_FILE` existence check,
   so a mock run without a creds file logs the misleading "Credentials file
   not found" warning. Swap the two checks.
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
Suite state: **249 passed, deterministic** across repeated runs. The
long-standing proxy-order flakiness is fixed on both sides — scraper-side
selection no longer shuffles (sticky assignment replaced it), and the
crawler-side tests assert set membership instead of a fixed order. The
crawler's own deliberate shuffle in `app/pipeline/extract_emails.py` is
unchanged. If proxy tests start failing again, check for leaked env
(`.env` injects `SCRAPER_PROXIES_FILE=proxies.txt`) before suspecting logic.

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
NULL provenance — see Open work #2/#3 for how to work around it and
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
