# email-scraper-verifer-cleaner — Review Notes

Living review + backlog. Updated 2026-07-29.

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

**2026-07-28 (CLI contract changes)** — from
`plans/code-review-2026-07-23.md`, all 15 findings fixed:

- `--strategy full-harvest` now **exits 2** when no variant set can be
  derived from `--query` and no `--queries` was supplied — i.e. the query
  names neither trade, or names both. Pass 2's multi-query sweep is
  full-harvest's entire coverage edge over grid, so running it on the base
  query alone burns full wall time for grid-level results. Fix: pass
  `--queries "v1,v2,..."`, or use `--grid`.
- `--queries` with any strategy other than `full-harvest` now **exits 2**
  instead of warning. `--bbox` / `--zip-csv` still only warn.
- `--min-contacts` / `--max-depth` default to `None` (effective defaults
  `DEFAULT_MIN_CONTACTS=500` / `DEFAULT_MAX_DEPTH=20` are applied inside
  `run_end_to_end_pipeline`). Both now warn when combined with
  grid/full-harvest, which ignore them. Both **exit 2** if non-positive.
- Industry classification is word-boundary regex: `AC`/`A/C`/`boiler`/
  `refrigeration`/`mini split`/`ductwork` classify as HVAC; `leak` alone
  no longer implies plumbing; a query naming both trades returns `None`
  (→ exit 2, see above) instead of resolving to whichever branch was
  checked first. One vertical per run — see "Answered / settled" #4.

**2026-07-29 (descriptive CSV filenames)**: local-CSV export fallback
(`write_leads_to_local_csv` / `export_new_leads`) no longer hardcodes
`data/leads_export.csv`. New `--csv-path` flag on both `run_pipeline.py`
and `run_zip_batch.py` overrides explicitly; otherwise
`run_pipeline._default_csv_path(query, location)` builds
`data/leads_<location-slug>_<query-slug>_<YYYY-MM-DD>.csv` (e.g.
`data/leads_sanjose_plumbing_2026-07-29.csv`). Batch runs
(`run_zip_batch.py`) omit the location segment since one batch spans many
ZIPs/cities under a single query — `data/leads_<query-slug>_<date>.csv`.
This only affects the CSV fallback path; Sheets exports are unaffected
(see #R1 for the separate mock-SPREADSHEET_ID short-circuit still open).

---

## Pipeline flow (as-built)

Three strategies via `--strategy {single-centroid, grid, full-harvest}`
on `run_pipeline.py` **and** on `run_zip_batch.py` (as of 2026-07-29 —
#R6). Default = `single-centroid` (legacy). Selection sugar: `--grid` ==
`--strategy grid`.

**Single-centroid** (legacy depth-loop). Delegates to
`run_location_pipeline()` — the same loop `run_zip_batch.py` uses, so
there is one depth-loop implementation and one definition of "enough
contacts":
```
init_db()
lat, lon, _bbox = geocode_location(location)      # ONCE via Nominatim
run_location_pipeline(query, location, lat=lat, lon=lon,   # centroid reused,
                      max_depth, target_new_exportable):   # not re-geocoded
  baseline = get_exportable_contact_count(dest)   # before the loop
  loop (depth 1 → max_depth, step +2):
    execute_scrape_and_ingest(query, location, lat, lon, depth)
    process_and_deduplicate_leads()
    harvest_emails_from_websites()                # must stay in-loop, see #R7
    new_exportable = get_exportable_contact_count(dest) - baseline
    if new_exportable >= target_new_exportable: break
    if depth >= max_depth: break
export_new_leads()
```
`stale_iterations_limit` is left `None` here, so single-centroid keeps its
legacy behavior of running to target-or-max-depth. `run_zip_batch.py`
passes `--stale-iterations` and does stop early on consecutive zero-yield
depth bumps.

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

All three strategies share one tail in `run_end_to_end_pipeline`:
```
if verify: verify_contacts_emails()               # --verify; failures warn
export_new_leads(min_score=min_score)             # --min-score N
```

---

## Strategy entrypoints (review #R6, shipped 2026-07-29)

Each strategy is one function in `run_pipeline.py`, and both CLIs call the
same three. `run_end_to_end_pipeline` is now geocode + dispatch + the
verify/export tail; it holds no strategy body of its own.

| Function | Strategy | Returns |
|---|---|---|
| `run_location_pipeline()` | single-centroid | `LocationRunMetrics` |
| `run_location_grid()` | grid | `LocationRunMetrics` |
| `run_location_full_harvest()` | full-harvest | `LocationRunMetrics` |

All three return the same `LocationRunMetrics` shape, so a batch caller
logs one line per row regardless of strategy. For the two non-looping
strategies, `_location_metrics()` snapshots the DB counts; `depths_run` is
a record of the passes (`(3,)` for grid, `(3, 10)` or `(3, 10, 3)` for
full-harvest — Pass 3 contributes one entry total, not one per ZIP) and
`stale_iterations` is 0 by construction, since a fixed set of passes has
no consecutive zero-yield depth bumps to count.

Grid and full-harvest **raise** when Nominatim returns no bounding box
rather than degrading to a centroid scrape — a silent downgrade would
report grid-strategy metrics for a single-centroid run.

### `run_zip_batch.py` flags

New: `--strategy` / `--grid` / `--cell-km` / `--queries`, matching
`run_pipeline.py`'s spelling and validation. `_resolve_strategy` and
`_resolve_query_variants` are imported from `run_pipeline`, not
reimplemented — both CLIs must agree on what `--grid` means and on when a
full-harvest is refused for lacking a variant set (exit 2), since that
check is the difference between a real sweep and grid-level results at
full wall cost.

- `--target-new-exportable` / `--max-depth` / `--stale-iterations` now
  default to `None` and are **single-centroid only** — they warn and are
  ignored under grid/full-harvest, which don't loop on depth. Non-positive
  values still exit 2. Effective defaults: 20 / `DEFAULT_MAX_DEPTH` / 2.
- `--cell-km` warns under single-centroid (no grid to size). Must be `> 0`.
- Geocoding: single-centroid geocodes inside `run_location_pipeline`;
  grid/full-harvest geocode in the batch loop to get each row's bbox. One
  Nominatim call per row either way.
- Batch full-harvest **never passes `zip_csv`** — Pass 3 is a fast ZIP
  top-up, and the batch already *is* the ZIP sweep. It warns once up front
  that each row costs a grid pass plus a multi-query centroid sweep.
- A row that fails (unmappable ZIP, scraper error) is logged and skipped;
  the batch continues, and export still runs once at the end.

Verification (`app/pipeline/verify_emails.py`) is wired into
`run_pipeline.py` and is the supported path — opt in with `--verify`.
When set, it runs after the email crawl and before export, for all three
strategies. Export can be gated by `--min-score N`. Reacher score map
(`_SCORE_BY_STATUS` in `verify_emails.py`): **safe=95, risky=50,
unknown=25, invalid=10**. Verifier failures are warnings, not fatal — an
unreachable Reacher instance scores everything `unknown` (25), so
`--min-score 50` on a dead verifier exports nothing. Archived
predecessor: `verify_emails_ARCHIVE.py` (BillionVerify — kept for
reference).

---

## Crawl-attempt ledger (review #R7, shipped 2026-07-29)

`harvest_emails_from_websites()` builds its skip-set from contacts with a
non-null email. Before the ledger, a business that was crawled and yielded
*nothing* left no trace, so it was indistinguishable from one never
crawled — re-fetched on every depth iteration and on every re-run against
the same DB. Two columns on `businesses` fix that:

| Column | Meaning |
|---|---|
| `last_crawled_at` | Stamped on **every** attempt — success, no-email, or exception. |
| `crawl_attempts` | Count of *consecutive* no-email attempts. Reset to 0 the moment a crawl yields an email. |

The pending set is now a three-way split in `extract_emails.py`: already
has an email → done; `crawl_attempts >= max_attempts` → given up;
`last_crawled_at` inside the cooldown → skip this round; else crawl. The
log line reports all four buckets.

Tuning (env, both optional):

- `CRAWL_RETRY_AFTER_HOURS` — cooldown before retrying a domain that
  yielded nothing. Default `720` (30 days): long enough that the depth
  loop within one run never re-crawls, short enough that a site which
  later publishes an address is picked up on a future run. `0` and
  non-integers are **invalid** here and fall back to the default — a
  zero-hour cooldown would restore the exact bug this prevents.
- `CRAWL_MAX_ATTEMPTS` — give up after N consecutive no-email attempts.
  Default `3`. `0` is valid and means no cap (cooldown still applies).

Crawl errors count as spent attempts on purpose — a domain that reliably
times out would otherwise be retried forever.

To force a full re-crawl:
`UPDATE businesses SET last_crawled_at = NULL, crawl_attempts = 0;`

---

Closed review items and shipped fixes now live in `CHANGELOG.md` (not
auto-loaded into context — read it on demand).

## TODO — next up

Ordered. Top item is the one to pick up first.

1. **#R1 Short-circuit the `mock` SPREADSHEET_ID** — stop attempting
   Sheets auth before falling back to CSV.
2. **#22 `robots.txt`** — still ignored. Per-host locking is in place.

## Still open (intentional deferrals)

### Review 2026-07-21 (commit 393a10c) — remaining backlog

Findings not covered by 393a10c.

- **#R1 SPREADSHEET_ID=`mock` still tries Sheets first** — `export_sheets.py`
  always calls `append_leads_to_google_sheets()` when SPREADSHEET_ID is
  "mock", fails auth, then falls through to CSV. Short-circuit when
  destination is the mock literal.
- **#R9 Crawler proxy = only proxy[0] from file** — Fixed. We now `random.shuffle(file_proxies)` and take the first one, meaning each pipeline run uses a fresh IP for crawling instead of burning the first IP in the file forever. Same for `run_scraper.py` which takes a random slice of 3.

### #12 — Export pushes empty-email rows to Sheets *(deferred by request)*

`export_sheets.py:129` — no `Contact.email IS NOT NULL` filter, so
phone-only placeholder contacts (`email = NULL`) get exported with a
blank Email column. Left as-is per project decision.

`run_pipeline.get_exportable_contact_count()` and the export query were
reconciled in a later fix; the remaining issue is the deliberate project
decision to allow blank-email rows to export.

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

**Automatic, as of 2026-07-29:** `init_db()` now runs
`_apply_additive_columns()` after `create_all()`, which `ALTER TABLE ... ADD
COLUMN`s any missing entry in `_ADDITIVE_COLUMNS` — currently
`businesses.last_crawled_at`, `businesses.crawl_attempts`, and
`export_history.exported_at` (added nullable on legacy SQLite DBs; backfill
existing rows once if you care about historical timestamps). `create_all()`
creates missing *tables* but never alters existing ones, so without this an
older DB file would raise "no such column" on the first harvest/export that
touched those fields. It is idempotent and additive-only; anything that
drops, renames, or backfills still belongs in the manual SQL below.

The rest is **manual**. If you have an existing SQLite `hvac_leads.db`:

```sql
-- Add processed_at column
ALTER TABLE raw_leads ADD COLUMN processed_at TIMESTAMP;

-- Add domain uniqueness (fails if you have dupes — clean them first)
CREATE UNIQUE INDEX ix_businesses_domain ON businesses(domain);

-- Add (business_id, email) composite unique on contacts
CREATE UNIQUE INDEX uq_contact_biz_email ON contacts(business_id, email);

-- Backfill legacy export_history rows after auto-adding exported_at
UPDATE export_history SET exported_at = CURRENT_TIMESTAMP WHERE exported_at IS NULL;

-- Disallow NULL contact_id on export_history if you are tightening legacy DBs
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

## Answered / settled

1. **Sheets export stays.** Default `SPREADSHEET_ID=mock` falls back to
   CSV, but the Sheets path is kept — decision 2026-07-28. Remaining
   nit is #R1 (short-circuit the `mock` literal before attempting auth).

2. **Category is not hardcoded.** `run_scraper.py:220` computes
   `effective_category = category if category else (query.strip() if
   query else None)`, and line 393 writes `category=category_str or
   effective_category` — so **scraper-reported categories win, with the
   query as fallback**, and an explicit `category=` kwarg overrides the
   query fallback. The old "`HVAC/Plumbing` hardcoded" claim was fixed
   in `393a10c`. Precedence kept as-is 2026-07-28.

3. **`min_contacts` = new exportable contacts produced by this run**
   (decided 2026-07-28), not cumulative DB contacts. Implemented as
   `get_exportable_contact_count()` minus a baseline captured before the
   depth loop, so re-running against a populated DB still scrapes.
   Single-centroid only — `grid` and `full-harvest` don't loop on depth
   and warn if the flag is passed. Note this forces the email crawl to
   stay inside the depth loop — the crawl-attempt ledger (#R7) is what
   keeps that affordable.

4. **One vertical per run.** A run is "plumbing in San Jose" *or* "HVAC
   in San Jose", never both. `_default_harvest_queries()` returns `None`
   for a query naming both trades, and full-harvest hard-errors (exit 2)
   telling the operator to split the run or pass `--queries` — it does
   not silently sweep both variant sets.

## Unclear / questions

1. **`max_depth=20`** — largely obsolete after 2026-07-20 experiment.
   Fast-mode caps at ~19 results per invocation regardless of depth (see
   `plans/scrape-strategy-experiments-2026-07-20.md`). Slow mode saturates
   ~depth 10 (~110 leads). Grid mode uses depth 3 per cell. Flag retained
   only for legacy `single-centroid` strategy compatibility.

2. **Crawler proxy rotation** — current crawler shuffles proxies from `CRAWLER_PROXY_FILE` so it's not locked to proxy[0]. 

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

As of 2026-07-28 `--min-contacts` uses the same baseline-delta count, so
it is "new this run" for both `run_zip_batch.py` and single-centroid
`run_pipeline.py` — they share one depth-loop implementation
(`run_location_pipeline`). `total_contacts` is still cumulative across
existing DB state and is reported for information only.

`new exportable` does **not** mean "new valid emails" or "new verified
emails". `export_sheets.py` exports any contact missing `export_history`
for the destination, including phone-only placeholder contacts with blank
email if they still exist (see #12).

Current data-quality caveat: harvested data can include suspicious junk
emails (`example@mysite.com`, `info@mysite.com`,
`wilvercasti@gami.com`). Spot-check exported CSV/Sheets before outreach or
verification, especially after broad ZIP sweeps.
