# email-scraper-verifer-cleaner — Review Notes

Living review + backlog. Updated 2026-07-21.

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

## Recently closed

- ✅ **Data-quality fixes** (2026-07-21, commit `393a10c`) — 4 review
  findings from lewis-test post-v2:
  - `run_scraper.py` derives `ScrapeRun.category` + `RawLead.category`
    from the query instead of hard-coding "HVAC/Plumbing". Optional
    `category=` kwarg override.
  - `process_leads.py` dedup dict skips domain rows in
    `{"http:", "https:", ""}` (legacy garbage seed that used to collapse
    website-less businesses onto one row).
  - `extract_emails.py` + `process_leads.py` expanded placeholder-email
    blocklist (mysite.com, gami.com, example.{com,org,net},
    yourdomain.com, wix*, sentry.io, godaddy.com, cloudflare/cloudfront/cdn).
    Filtered on BOTH scraper-side and crawler-side paths.
  - `run_pipeline.py` `--verify` flag wires Reacher between harvest and
    export. `--min-score N` gates export by verification score. Verifier
    failures are warnings, pipeline continues.
- ✅ **Coverage v2** (2026-07-21) `--strategy full-harvest`: grid + multi-query
  slow at centroid + optional fast ZIP top-up. SJ 2026-07-20 experiment showed
  504 unique biz vs 362 for grid alone (+39%). Default 8-query variant list
  chosen from experiment's per-variant marginal-lift table.
- ✅ **Coverage v1** (2026-07-21) `--grid --cell-km <km>` via scraper's native
  `-grid-bbox`. JS mode via Playwright. 429 biz on SJ 2026-07-20 (vs 95 for
  28-ZIP sweep, 17 for single centroid). Requires
  `scripts/setup_scraper_playwright.sh` one-time driver install + version-hack.
- ✅ **Bug** (2026-07-20) `extract_emails.py:247` — `ExportHistory` imported
  now. Crash was silent unless a placeholder contact was hit during crawl.
- ✅ **Critical #1** OS-aware scraper binary path (`.exe` on Windows, bare on unix)
- ✅ **Critical #2** `create_all()` moved into `init_db()`, called from `run_pipeline`
- ✅ **Critical #3** `processed_at` flag on `raw_leads` — no more quadratic reprocessing
- ✅ **Critical #4** `requirements.txt` rewritten as UTF-8, garbage pkgs dropped, `google-api-python-client` added
- ✅ **Critical #5** Email regex TLD loosened from `{2,4}` → `{2,}`
- ✅ **Verifier** Old BillionVerify code archived; new `verify_emails.py` calls
  self-hosted Reacher at `http://104.128.66.74:8080/v0/check_email`. Deploy
  scripts + `KAMATERA_ACCESS_KEY`/`KAMATERA_SECRET_KEY` live in sibling repo
  `autopilotlocal/email-verifier` (see its `CLAUDE.md`).
- ✅ **Medium #6** `database.py` raises clear `RuntimeError` if `DATABASE_URL` unset
- ✅ **Medium #7** `extract_emails` now crawls `/contact`, `/contact-us`, `/about`, `/about-us`, `/team` in addition to homepage
- ✅ **Medium #8** `ThreadPoolExecutor(max_workers=10)` with per-host lock + 0.75s intra-host delay
- ✅ **Medium #9** `geocode_location` called ONCE from `run_pipeline`, lat/lon threaded into every scrape iteration
- ✅ **Medium #10** N+1 queries killed — `process_leads` preloads businesses + contact fingerprints into dicts up front
- ✅ **Medium #11** Raw email field split on `[,;\s]+` and validated against `EMAIL_REGEX` before insert
- ✅ **Medium #13** DB constraints + indexes:
  - `businesses.domain` UNIQUE
  - `(contacts.business_id, contacts.email)` composite UNIQUE
  - Indexes on all FK columns + `raw_leads.processed_at`, `contacts.lead_status`, `export_history` composite
- ✅ **Cleanup #15** Old BillionVerify verifier archived; new Reacher-based `verify_emails.py` runs against Kamatera instance
- ✅ **Cleanup #17** Central logging (`app/logging_config.py`) — every module uses `logger` instead of `print()`; `LOG_LEVEL` env var controls verbosity
- ✅ **Cleanup #19** `tests/` suite added — 48 passing unit tests covering `extract_domain`, `normalize_phone`, `_parse_and_validate_emails`, `extract_emails_from_html`, and Reacher response handling. Caught a real bug: `extract_domain` was case-sensitive on the scheme check (fixed inline).
- ✅ **Cleanup #21** `os.makedirs` guarded against empty `dirname` for bare-filename CSV paths
- ✅ **Cleanup #23** Scraper subprocess now has a 30-min hard timeout (override via `SCRAPER_TIMEOUT_SEC` env var); `TimeoutExpired` logs and propagates

---

## Still open (intentional deferrals)

### Review 2026-07-21 (commit 393a10c) — remaining backlog

Ordered by priority. Findings not covered by 393a10c.

- **#R1 SPREADSHEET_ID=`mock` still tries Sheets first** — `export_sheets.py`
  always calls `append_leads_to_google_sheets()` when SPREADSHEET_ID is
  "mock", fails auth, then falls through to CSV. Short-circuit when
  destination is the mock literal.
- **#R2 `--queries` warning fires but arg still parsed + passed** — set
  `query_variants=None` when strategy ≠ full-harvest. Currently ignored
  downstream but misleading.
- **#R3 Full-harvest Pass 3 no per-ZIP logging** — silent for-loop over
  ZIPs. Add `logger.info("[%d/%d] %s", i, len(zip_rows), zip_loc)` before
  each scrape call.
- **#R4 `min_contacts` still cumulative + dead in grid/full-harvest** —
  in non-legacy strategies it's only logged, never gates. Either rename
  to `--target-new-exportable` (matches `run_zip_batch.py`) and apply
  consistently, or delete flag from non-legacy strategies + document.
- **#R5 `DEFAULT_HARVEST_QUERIES` plumbing-only** — HVAC / other
  industries need different variants. Options: key by industry, require
  `--queries` when strategy=full-harvest, or move defaults to a config
  file.
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

⚠ Reconcile inconsistency: `run_pipeline.get_exportable_contact_count()`
does filter `Contact.email.isnot(None)`, but the actual export query in
`export_new_leads()` matches now (post-Round 2 fix). Verify the count
and export queries agree before next release.

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
