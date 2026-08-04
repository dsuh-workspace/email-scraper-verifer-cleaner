# email-scraper-verifer-cleaner — Changelog

Shipped changes, closed review items, and archived history that no longer
belongs in `CLAUDE.md`. `CLAUDE.md` is the current operator guide; this
file is the dated record.

## 2026-08-03

- ✅ **Operator guide trimmed and split from history** — `CLAUDE.md` now
  stays focused on current strategy contracts, runbook details, and open
  work. Historical release notes and closed review items live here.
- ✅ **Scraper auto-update formalized** — `scripts/update_scraper.sh` now
  pulls `../google-maps-scraper`, rebuilds to a temp path, smoke-tests the
  new binary, and only swaps it into `app/scraper/google-maps-scraper` if
  output remains compatible with `run_scraper.py`. Failed builds or schema
  mismatches leave the existing binary untouched.
- ✅ **Weekly launchd automation for scraper updates** — launch agent
  `com.apl.update-scraper` runs Sundays at 03:17. Tracked plist:
  `scripts/com.apl.update-scraper.plist`. Launchd logs go to
  `logs/update_scraper.launchd.log`; script logs go to
  `logs/update_scraper.log`.
- ✅ **Local Reacher verifier replaces Kamatera as the main path** —
  verification now runs against a local instance at
  `http://127.0.0.1:8080/v0/check_email`, with
  `scripts/start_local_verifier.sh` / `scripts/stop_local_verifier.sh`
  managing the local service.

## 2026-08-02

- ✅ **Full-harvest Pass 2 no longer defaults to combined multi-query mode**
  — the vendored Go scraper shared a deduper/exiter across variants, which
  silently dropped leads in combined mode. Python-side fix: per-variant
  subprocesses (`--pass2-per-variant`) are now the default; old combined
  behavior survives only as opt-in diagnostics via `--pass2-combined`.
- ✅ **HVAC default full-harvest query set pruned from 8 → 3 variants** —
  San Jose rerun evidence showed the other 5 variants contributed roughly no
  net-new businesses per variant.
- ✅ **Plumbing default full-harvest query set pruned from 8 → 2 variants**
  — fresh San Jose plumbing rerun data showed Pass 2 lift concentrated in
  `Plumbing` and `Plumber`; the other six variants were empty or missing on
  that rerun.
- ✅ **Future lift-table work redirected to provenance fields** — follow-up
  focus shifted from raw-lead first-seen inference to using
  `businesses.first_scrape_run_id` and `contacts.first_scrape_run_id` for
  downstream attribution.

## 2026-08-01

- ✅ **#R9 Crawler proxy no longer always uses proxy[0]** (commit `9d28a8a`)
  — `random.shuffle(file_proxies)` before selecting the first crawler
  proxy, and `run_scraper.py` now takes a random slice of 3.

## 2026-07-29

- ✅ **#R6 `run_zip_batch.py --strategy`** — metro-wide grid/full-harvest
  runs are now reachable from the CSV runner. Strategy bodies were extracted
  from `run_end_to_end_pipeline` into `run_location_pipeline()`,
  `run_location_grid()`, and `run_location_full_harvest()`, and both CLIs
  now call the same three functions.
  - All three return `LocationRunMetrics`.
  - `_resolve_strategy` / `_resolve_query_variants` are imported into
    `run_zip_batch.py`, not copied.
  - Batch full-harvest deliberately omits `zip_csv` because the batch
    itself is the ZIP sweep.
  - Grid/full-harvest raise when Nominatim returns no bbox instead of
    silently degrading to centroid mode.
  - Tests: 15 new; full suite reached 181 passing.
- ✅ **#R7 crawl-attempt ledger** — `harvest_emails_from_websites()` no
  longer re-crawls businesses that previously yielded no email.
  - Added `businesses.last_crawled_at` and `businesses.crawl_attempts`.
  - Tuned by `CRAWL_RETRY_AFTER_HOURS` and `CRAWL_MAX_ATTEMPTS`.
  - `init_db()` gained `_apply_additive_columns()` so older DBs pick up the
    additive columns automatically.
  - The crawl stays inside the depth loop because `--min-contacts` counts
    new exportable contacts, which require emails.
  - Tests: 14 new; full suite 166 passing.
- ✅ **`Contact`/`ExportHistory` NameError in `_persist_emails_for_business`**
  — pre-existing live bug found while verifying #R7. Successful crawls that
  found an email crashed on `NameError`; fixed with a local import matching
  the module's deferred-import pattern.
- ✅ **Descriptive CSV fallback filenames** — local CSV export no longer
  hardcodes `data/leads_export.csv`. `--csv-path` overrides explicitly;
  otherwise defaults are:
  - single-location runs:
    `data/leads_<location-slug>_<query-slug>_<YYYY-MM-DD>.csv`
  - batch runs:
    `data/leads_<query-slug>_<YYYY-MM-DD>.csv`

## 2026-07-28

- ✅ **Architecture/doc alignment follow-up**
  - One vertical per run. `_default_harvest_queries()` returns `None` for a
    query naming both plumbing and HVAC; full-harvest exits 2 unless the
    operator splits the run or passes explicit `--queries`.
  - Single-centroid now delegates to `run_location_pipeline()` so there is
    one depth-loop implementation and one definition of “enough contacts”.
  - `--min-contacts` means new exportable contacts produced by this run,
    not cumulative DB contacts.
  - Non-positive `--min-contacts` / `--max-depth` exit 2.
  - `scripts/harvest_best.py` moved under `scripts/experiments/` and is
    documented as offline-only.
  - Docs corrected: Reacher score map is `safe=95, risky=50, unknown=25,
    invalid=10`; README no longer says verification is unwired; stale
    category notes were removed.
  - #R7 was re-scoped from “move crawl out of depth loop” to the actual
    waste: re-crawling no-email businesses.
- ✅ **Review 2026-07-23 items #1–#15 fixed**
  - `_default_harvest_queries()` rewritten with word-boundary regex so HVAC
    and plumbing classification no longer depend on substring accidents.
  - Full-harvest now exits 2 when it cannot derive a variant set and no
    `--queries` was supplied.
  - `queries=()` raises instead of silently reverting to defaults.
  - `--min-contacts` / `--max-depth` default to `None`, so scope warnings
    reflect whether the operator actually passed the flag.
  - `--queries` with non-full-harvest strategies is now an error.
  - Pass 3 log lines use one `[i/N zip Z]` shape.
  - Tests added for degraded Pass 2 behavior, CLI hard errors, ignored-flag
    warnings, and classifier edge cases.
- ✅ **#R0 `run_location_pipeline` NameError** (commit `d8df29f`) — single-
  centroid and ZIP-batch paths were passing scraper tuning args that were
  not in scope. Fixed by threading the kwargs through callers instead of
  reverting the tuning work.
- ✅ **#14 dead imports in `create_tables.py`** — closed for bookkeeping
  after the schema rewrite; no separate patch was needed.

## 2026-07-22

- ✅ **Review 393a10c items R2/R3/R4/R5**
  - `main()` now only builds `query_variants` for `full-harvest`.
  - Full-harvest Pass 3 logs `[i/N] scraping <zip_loc>` before each ZIP.
  - Grid/full-harvest completion logs drop the misleading target clause.
  - `_default_harvest_queries(query)` was introduced to pick plumbing/HVAC
    defaults by keyword, with explicit warning fallback for unknown
    industries.

## 2026-07-21

- ✅ **Data-quality fixes** (commit `393a10c`)
  - `run_scraper.py` derives `ScrapeRun.category` + `RawLead.category`
    from the query instead of hard-coding `HVAC/Plumbing`.
  - `process_leads.py` no longer lets invalid domains like `http:` collapse
    website-less businesses into one row.
  - Placeholder-email blocklist expanded across scraper and crawler paths.
  - `run_pipeline.py --verify` now wires Reacher between harvest and export;
    `--min-score N` gates export by verification score.
- ✅ **Coverage v2: `--strategy full-harvest`** — grid + multi-query slow
  centroid + optional fast ZIP top-up. San Jose 2026-07-20 experiment:
  504 unique businesses vs 362 for grid alone (+39%).
- ✅ **Coverage v1: `--grid --cell-km <km>`** — native grid-bbox scraping
  via Playwright-backed JS mode. San Jose 2026-07-20 experiment: 429
  businesses vs 95 for 28-ZIP sweep and 17 for single centroid.

## 2026-07-20 and earlier

- ✅ **Bug** `extract_emails.py:247` — `ExportHistory` import fixed.
- ✅ **Critical #1** OS-aware scraper binary path.
- ✅ **Critical #2** `create_all()` moved into `init_db()`, called from
  `run_pipeline`.
- ✅ **Critical #3** `processed_at` added to `raw_leads` to stop quadratic
  reprocessing.
- ✅ **Critical #4** `requirements.txt` rewritten as UTF-8, garbage
  packages dropped, `google-api-python-client` added.
- ✅ **Critical #5** Email regex TLD loosened from `{2,4}` to `{2,}`.
- ✅ **Verifier** Old BillionVerify path archived; Reacher-based verifier
  introduced first against the Kamatera instance.
- ✅ **Medium #6** `database.py` raises a clear `RuntimeError` when
  `DATABASE_URL` is unset.
- ✅ **Medium #7** `extract_emails` now crawls common contact/about/team
  pages in addition to the homepage.
- ✅ **Medium #8** `ThreadPoolExecutor(max_workers=10)` plus per-host lock
  and 0.75s intra-host delay.
- ✅ **Medium #9** `geocode_location` called once from `run_pipeline` and
  lat/lon threaded through scrape iterations.
- ✅ **Medium #10** N+1 queries removed by preloading businesses and
  contact fingerprints.
- ✅ **Medium #11** Raw email field split and validated before insert.
- ✅ **Medium #13** DB constraints and indexes added:
  - `businesses.domain` UNIQUE
  - `(contacts.business_id, contacts.email)` composite UNIQUE
  - indexes on FK columns plus `raw_leads.processed_at`,
    `contacts.lead_status`, and the `export_history` composite
- ✅ **Cleanup #15** Old BillionVerify verifier archived.
- ✅ **Cleanup #17** Central logging via `app/logging_config.py`.
- ✅ **Cleanup #19** Initial `tests/` suite added; caught a real bug in
  scheme case-sensitivity inside `extract_domain`.
- ✅ **Cleanup #21** `os.makedirs` guarded for bare-filename CSV paths.
- ✅ **Cleanup #23** Scraper subprocess now has a 30-minute hard timeout,
  overridable via `SCRAPER_TIMEOUT_SEC`.
