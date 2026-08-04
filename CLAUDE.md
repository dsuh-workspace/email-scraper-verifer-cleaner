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

**Single-centroid** delegates to `run_location_pipeline()` and keeps the
legacy depth loop:

```text
init_db()
lat, lon, _bbox = geocode_location(location)
run_location_pipeline(query, location, lat=lat, lon=lon,
                      max_depth, target_new_exportable):
  baseline = get_exportable_contact_count(dest)
  loop (depth 1 → max_depth, step +2):
    execute_scrape_and_ingest(query, location, lat, lon, depth)
    process_and_deduplicate_leads()
    harvest_emails_from_websites()
    new_exportable = get_exportable_contact_count(dest) - baseline
    if new_exportable >= target_new_exportable: break
    if depth >= max_depth: break
export_new_leads()
```

`run_zip_batch.py` can add `--stale-iterations`; single-centroid in
`run_pipeline.py` still runs to target-or-max-depth.

**Grid** runs one bbox-based scrape, then dedupe/crawl/export:

```text
init_db()
lat, lon, bbox = geocode_location(location)
execute_scrape_and_ingest(query, location, bbox=bbox, cell_km=2.0, depth=3)
process_and_deduplicate_leads()
harvest_emails_from_websites()
export_new_leads()
```

**Full-harvest** runs three passes:

```text
init_db()
lat, lon, bbox = geocode_location(location)

# PASS 1: grid single-query
execute_scrape_and_ingest(query, location, bbox=bbox, cell_km, depth=3)
process_and_deduplicate_leads()

# PASS 2: per-variant slow at centroid
for variant in DEFAULT_HARVEST_QUERIES:
  execute_scrape_and_ingest(variant, location, lat, lon, depth=10,
                            fast_mode=False)
  process_and_deduplicate_leads()

# PASS 3 (optional): fast ZIP top-up
for row in load_zip_csv(zip_csv):
  zlat, zlon, _ = geocode_location(f"{city}, {state}, {zip}")
  execute_scrape_and_ingest(query, zip_loc, lat=zlat, lon=zlon,
                            depth=3, fast_mode=True)
process_and_deduplicate_leads()

harvest_emails_from_websites()
export_new_leads()
```

All strategies share the same tail in `run_end_to_end_pipeline`:

```text
if verify: verify_contacts_emails()
export_new_leads(min_score=min_score)
```

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
Export can be gated by `--min-score N`. Reacher score map:
**safe=95, risky=50, unknown=25, invalid=10**. Verifier failures warn but
are not fatal.

## Crawl-attempt ledger

`harvest_emails_from_websites()` uses two `businesses` columns to avoid
repeatedly crawling no-yield domains:

| Column | Meaning |
|---|---|
| `last_crawled_at` | Stamped on every attempt — success, no-email, or exception |
| `crawl_attempts` | Count of consecutive no-email attempts; resets to 0 on success |

Pending set in `extract_emails.py`: already has email → done;
`crawl_attempts >= max_attempts` → given up; `last_crawled_at` inside the
cooldown → skip; else crawl.

Tuning:

- `CRAWL_RETRY_AFTER_HOURS`: default `720` (30 days). `0` and non-integers
  are invalid and fall back to default.
- `CRAWL_MAX_ATTEMPTS`: default `3`. `0` means no cap.

Crawl errors count as spent attempts on purpose.

To force a full re-crawl:
`UPDATE businesses SET last_crawled_at = NULL, crawl_attempts = 0;`

## Run tracking

**Check `RUNS.md` before starting a new city/vertical run.** Update it
after any real production run completes.

## Open work

1. **#R1 Short-circuit the `mock` SPREADSHEET_ID** so Sheets auth is not
   attempted before CSV fallback.
2. **#22 `robots.txt`** is still ignored. Per-host locking is in place.
3. **Use provenance fields for future lift tables**: rely on
   `businesses.first_scrape_run_id` / `contacts.first_scrape_run_id`
   instead of raw-lead first-seen inference.

## Intentional deferrals

- **#12 Export pushes empty-email rows to Sheets** — deliberate project
  decision. Blank-email contacts may still export.
- **#20 Commits inside per-business loop** — batching every 25 is
  acceptable for now.
- **#22 No `robots.txt` / no per-domain politeness** — per-host locking is
  in place; `robots.txt` is still ignored.

## Environment / operational

### Python / venv conventions

- Repo pins Python via `.python-version` = `3.12.9`.
- Run Python commands inside `.venv`.
- Canonical setup:
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- Tests and CLI entrypoints should be run from activated `.venv`.

### Local Reacher instance

- URL: `http://127.0.0.1:8080/v0/check_email`
- `./scripts/start_local_verifier.sh` starts it. It no-ops if already
  reachable, restarts an existing stopped `reacher-backend` container when
  possible, prefers Docker, and falls back to building
  `../email-verifier/backend` from source if Docker is unavailable.
- `./scripts/stop_local_verifier.sh` stops and removes the container.
- `REACHER_API_URL` is set to this local URL in `.env` and
  `verify_emails.py`.
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

This is idempotent and additive-only. Backfills and non-additive changes
still belong in manual SQL.

Manual SQL for legacy SQLite DBs when needed:

```sql
ALTER TABLE raw_leads ADD COLUMN processed_at TIMESTAMP;
CREATE UNIQUE INDEX ix_businesses_domain ON businesses(domain);
CREATE UNIQUE INDEX uq_contact_biz_email ON contacts(business_id, email);
UPDATE export_history SET exported_at = CURRENT_TIMESTAMP WHERE exported_at IS NULL;
```

`process_and_deduplicate_leads()` copies `RawLead.scrape_run_id` onto new
`Business` / `Contact` rows as `first_scrape_run_id` for future lift-table
attribution.

Legacy bad-domain check:

```sql
SELECT id, business_name, domain
FROM businesses
WHERE domain = 'http:' OR domain LIKE 'http:%';
```

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
