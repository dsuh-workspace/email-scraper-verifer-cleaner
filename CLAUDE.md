# email-scraper-verifer-cleaner — Review Notes

Living review + backlog. Updated 2026-07-22.

Purpose: HVAC/Plumbing lead-gen pipeline. Scrapes Google Maps → SQL →
dedupes → crawls sites for emails → optionally verifies via self-hosted
Reacher on Kamatera → exports Sheets/CSV.
Now captures rich map details (Review Count, Review Rating, Address, Status, Description, Place ID).

**2026-07-21 (v1)**: pipeline supports native grid-mode scraping via
`--grid --cell-km <km>` on `run_pipeline.py`. Empirically 4-25× coverage
of single-centroid mode. Requires Playwright driver installed via
`./scripts/setup_scraper_playwright.sh` (one-time, ~265 MB). See
`plans/generalized-city-coverage-method-2026-07-20.md` for the full
strategy write-up + n=2 SJ/SC empirical results.

**2026-07-21 (v2)**: pipeline adds `--strategy full-harvest` = grid +
multi-query slow at centroid + optional fast ZIP top-up (via
`--zip-csv`). On SJ 2026-07-20 experiment: grid alone = 362 biz, full
harvest = 504 biz (+39%). Default queries = 8 plumbing variants; override
with `--queries "a,b,c"`. See
`plans/scrape-strategy-experiments-2026-07-20.md` for full n=1 evidence.

---

## Pipeline flow (as-built)

Three strategies via `--strategy {single-centroid, grid, full-harvest}`
on `run_pipeline.py`. Default = `single-centroid` (legacy). Selection
sugar: `--grid` == `--strategy grid`.

**Single-centroid** (legacy depth-loop):
```
init_db()
lat, lon = geocode_location(location)             # ONCE via Nominatim
loop (depth 1 → max_depth, step +2):
  execute_scrape_and_ingest(query, location, lat, lon, depth)
  process_and_deduplicate_leads()
  harvest_emails_from_websites()
  if contact_count >= min_contacts: break
export_new_leads()
```

**Grid** (v1, JS mode, requires Playwright):
```
init_db()
lat, lon, bbox = geocode_location(location)       # bbox from Nominatim
                                                  # or --bbox override
execute_scrape_and_ingest(query, location,
                          bbox=bbox, cell_km=2.0, depth=3)
process_and_deduplicate_leads()
harvest_emails_from_websites()
export_new_leads()
```

**Full-harvest** (v2, three passes):
```
init_db()
lat, lon, bbox = geocode_location(location)

# PASS 1: grid single-query
execute_scrape_and_ingest(query, location, bbox=bbox, cell_km, depth=3)
process_and_deduplicate_leads()

# PASS 2: multi-query slow at centroid (8 variants in one input file)
execute_scrape_and_ingest(query, location, lat, lon, depth=10,
                          queries=DEFAULT_HARVEST_QUERIES, fast_mode=False)
process_and_deduplicate_leads()

# PASS 3 (optional): fast ZIP top-up
for row in load_zip_csv(zip_csv):
  zlat, zlon, _ = geocode_location(f"{city}, {state}, {zip}")
  execute_scrape_and_ingest(query, zip_loc, lat=zlat, lon=zlon,
                            depth=3, fast_mode=True)
process_and_deduplicate_leads()

harvest_emails_from_websites()                    # one crawl at end
export_new_leads()
```

Verification (`app/pipeline/verify_emails.py`) is now opt-in via
`--verify` on `run_pipeline.py`. When set, it runs after crawl and
before export. Export can be gated by `--min-score N` (Reacher scores:
safe=100, risky=50, unknown=25, invalid=0). Archived predecessor:
`verify_emails_ARCHIVE.py` (BillionVerify — kept for reference).

---

Closed review items and shipped fixes now live in `CHANGELOG.md` (not
auto-loaded into context — read it on demand).

## Still open (intentional deferrals)

### Review 2026-07-21 (commit 393a10c) — remaining backlog

Ordered by priority. Findings not covered by 393a10c.

- **#R1 SPREADSHEET_ID=`mock` still tries Sheets first** — `export_sheets.py`
  always calls `append_leads_to_google_sheets()` when SPREADSHEET_ID is
  "mock", fails auth, then falls through to CSV. Short-circuit when
  destination is the mock literal.
- **#R6 `run_zip_batch.py` still on legacy `run_location_pipeline`** —
  no `--strategy` support. Metro-wide full-harvest via batch is the real
  coverage play; without it, `run_zip_batch.py` = orphaned path.
- **#R7 Single-centroid crawls every depth iteration; grid/full-harvest
  crawl once at end** — inconsistent + wasted HTTP. Match single-centroid
  to the grid/full-harvest shape (crawl once after loop breaks).
- **#R8 `harvest_best.py` diverges from pipeline** — same 3-pass strategy
  but no DB, no dedup, no crawl. Either delete (now that
  `--strategy full-harvest` exists in `run_pipeline.py`) or move to
  `scripts/experiments/` + document as offline-only.
- **#R9 Crawler proxy = only proxy[0] from file** — 10 workers × 1 IP
  across ~350 domains = fingerprint risk. Deferred until block signals
  appear (per existing #22). Rotate sticky-per-host when the time comes:
  `proxies[hash(host) % len(proxies)]`.
- **#R10 Untracked cruft to clean before merge** —
  `proxies_old.txt`, `query.txt`, `run_tests.sh`, `sql_add_columns2.sql`,
  `test_output.json`, `test_query.txt`, `test_results.json`. Delete or
  `.gitignore`.

### #12 — Export pushes empty-email rows to Sheets *(deferred by request)*

`export_sheets.py:129` — no `Contact.email IS NOT NULL` filter, so
phone-only placeholder contacts (`email = NULL`) get exported with a
blank Email column. Left as-is per project decision.

`run_pipeline.get_exportable_contact_count()` and the export query were
reconciled in a later fix; the remaining issue is the deliberate project
decision to allow blank-email rows to export.

### #14 — Dead imports in `create_tables.py`

Post-rewrite: none remain. Closed by natural attrition during the #13
schema rewrite.

### #16 — CLI validation follow-up for `run_pipeline.py`

`argparse` is now in place. Future hardening: reject invalid non-positive
values for:

- `--min-contacts <= 0`
- `--max-depth <= 0`

Likely shape:

```python
if args.min_contacts <= 0:
    parser.error("--min-contacts must be > 0")
if args.max_depth <= 0:
    parser.error("--max-depth must be > 0")
```

### #20 — Commits inside per-business loop

`extract_emails.py` batches every 25, which is acceptable. Not worth
further tuning until we see real throughput numbers.

### #22 — No `robots.txt` / no per-domain politeness

Half-fixed: per-host locking is in place, `robots.txt` still ignored.
Reasonable given we're crawling only shortlisted contact pages.

---

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
- Tests and CLI entrypoints (`run_pipeline.py`, `run_zip_batch.py`) should
  be run from activated `.venv`, not arbitrary system Python.

### Kamatera Reacher instance

- URL: `http://104.128.66.74:8080/v0/check_email`
- No auth on the endpoint itself; `KAMATERA_ACCESS_KEY` /
  `KAMATERA_SECRET_KEY` are only consumed by the deploy scripts in the
  `autopilotlocal/email-verifier` repo.
- Server is single-instance, no LB — if it goes down, verification
  fails silently to `"unknown"` (score 25). Redeploy via
  `deploy_kamatera.sh` in the sibling repo.
- From this laptop right now, the endpoint is unreachable (network
  timeout to `104.128.66.74:8080`). Test from the target deploy
  environment or check Kamatera server status via `list_servers.sh` in
  the sibling repo.

### DB migration for `processed_at` + new constraints

If you have an existing SQLite `hvac_leads.db`:

```sql
-- Add processed_at column
ALTER TABLE raw_leads ADD COLUMN processed_at TIMESTAMP;

-- Add domain uniqueness (fails if you have dupes — clean them first)
CREATE UNIQUE INDEX ix_businesses_domain ON businesses(domain);

-- Add (business_id, email) composite unique on contacts
CREATE UNIQUE INDEX uq_contact_biz_email ON contacts(business_id, email);

-- For newer export_history rows, add timestamp + disallow NULL contact_id
ALTER TABLE export_history ADD COLUMN exported_at TIMESTAMP;
UPDATE export_history SET exported_at = CURRENT_TIMESTAMP WHERE exported_at IS NULL;
```

Manual follow-up for existing DBs:

```sql
-- Inspect legacy bad domains from old case-sensitive scheme handling
SELECT id, business_name, domain
FROM businesses
WHERE domain = 'http:' OR domain LIKE 'http:%';
```

Or nuke `database/hvac_leads.db` and re-run — `init_db()` builds
everything.

---

## Unclear / questions

1. **Sheets export live?** Default `SPREADSHEET_ID=mock` → always
   CSV fallback. Are we ever really pushing to Sheets, or should the
   Sheets path be deleted?

2. **`HVAC/Plumbing` hardcoded** as category in `run_scraper.py` even
   when `query` says otherwise. Derive from `query` or scraper output.

3. **`min_contacts` semantics** — total DB contacts vs new-this-run?
   Currently total, so re-running on a full DB never scrapes anything.
   Probably wants "new this run". Only affects `single-centroid`
   strategy; `grid` and `full-harvest` don't loop on depth.

4. **`max_depth=20`** — largely obsolete after 2026-07-20 experiment.
   Fast-mode caps at ~19 results per invocation regardless of depth (see
   `plans/scrape-strategy-experiments-2026-07-20.md`). Slow mode saturates
   ~depth 10 (~110 leads). Grid mode uses depth 3 per cell. Flag retained
   only for legacy `single-centroid` strategy compatibility.

5. **Verifier exists but is not wired into `run_pipeline`** — deliberate
   as of now. `app/pipeline/verify_emails.py` is implemented and runnable
   manually, but `run_pipeline.py` does not call it inside scrape /
   process / harvest loop or before export. If we want inline
   verification later, decide when it should run (each iteration vs once
   at end) and whether cost / latency is acceptable.

6. **Crawler proxy rotation** — current crawler uses only first proxy from
   `CRAWLER_PROXY_FILE`. Future improvement if site blocking appears:
   implement rotation strategy (prefer sticky-per-host over pure random)
   so website crawling can spread load without breaking per-host politeness.

---

## Batch ZIP yield / count semantics

San Jose ZIP sweeps on 2026-07-20 showed strong diminishing returns after
first few ZIPs. `95110` produced `new_exportable=22`, `95111` produced
`16`, then many later ZIPs produced `0-6` and several stopped early on
stale iterations (`95113`, `95120`, `95122`, `95123`, `95128`, `95130`).
Repeated `Added 0 new businesses`, `Added 0 new contacts`, and
`Harvested 0 unique email contacts` lines are normal signal that nearby
ZIPs are overlapping same market, not necessarily pipeline failure.

For batch evaluation, prefer `new_exportable_contacts` over
`total_contacts`. In `run_pipeline.py`, `get_contact_count()` returns
cumulative DB-wide contact count, while `get_exportable_contact_count()`
counts contacts not yet exported for destination and
`new_exportable_contacts` is computed as current exportable minus baseline
for that location run.

Important consequence: `total_contacts` and legacy `--min-contacts`
semantics are cumulative across existing DB state, not "new this ZIP" or
"new this run" counts. `new exportable` also does **not** mean "new valid
emails" or "new verified emails". `export_sheets.py` exports any contact
missing `export_history` for destination, including phone-only placeholder
contacts with blank email if they still exist.

Current data-quality caveat: harvested data can include suspicious junk
emails (`example@mysite.com`, `info@mysite.com`,
`wilvercasti@gami.com`). Spot-check exported CSV/Sheets before outreach or
verification, especially after broad ZIP sweeps.
