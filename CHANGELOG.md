# email-scraper-verifer-cleaner — Changelog

Shipped changes, closed review items, and archived history that no longer
belongs in `CLAUDE.md`. `CLAUDE.md` is the current operator guide; this
file is the dated record.

## 2026-08-07

- 🐛 **Orphaned scraper processes no longer outlive a killed parent** (was
  `CLAUDE.md` open work #3). `execute_scrape_and_ingest()` switched from
  `subprocess.run()` to `subprocess.Popen()` started in its own process
  group (`start_new_session=True` on POSIX, `CREATE_NEW_PROCESS_GROUP` on
  Windows). On timeout, Ctrl-C, or any other exception while the scraper is
  running, `_kill_scraper_process_group()` kills the whole group
  (`os.killpg`), not just the Go binary's own pid — the previous code only
  ever killed the direct child, leaving Playwright's Chromium grandchildren
  running and burning proxy bandwidth after the pipeline had died (2.0 GB
  at a 39% failure rate, found 2026-08-07). This can't help if the parent
  Python process itself gets `SIGKILL`ed — nothing in it runs then — only
  Ctrl-C and exceptions the process can still unwind through. `execute_
  scrape_and_ingest()` also now logs (not kills) any pre-existing
  `google-maps-scraper` process via `pgrep -f` before starting, so a stale
  orphan surfaces immediately instead of only being found by chance later.
  Tests in `test_run_scraper.py`, `test_timeout_salvage.py`,
  `test_block_detection_integration.py` updated to fake `subprocess.Popen`
  instead of `subprocess.run`. Suite: 287 passed. Remaining Open work items
  renumbered: old #4→3, #5→4, #6→5, #7→6, #8→7.

## 2026-08-06

- ✅ **`export_new_leads()` / `export_run_outputs()` gain an optional
  `run_cohort_start` param** (was `CLAUDE.md` open work #4). Filters on
  `businesses.first_scrape_run_id >= run_cohort_start`, reaching all three
  output files — including `_all`, previously unscoped by design.
  `scripts/analysis/export_cohort.py` (its docstring names the incident
  this closes: 76/166 of a supposedly-Sunnyvale/Santa-Clara cohort export
  turned out to be San Jose) remains the tool for historical cohorts, since
  it also falls back to `MIN(raw_leads.scrape_run_id)` for NULL-provenance
  businesses; the new parameter does not. Neither CLI wires it to a flag
  yet — call the functions directly. Tests in `TestRunCohortFilter`.
- ♻️ **`_scraper_proxy_args()` collapsed into the production cmd-building
  path** (was `CLAUDE.md` open work #6). It and `execute_scrape_and_ingest`
  each formatted a selected proxy list into the upstream `-proxies` flag
  separately; the formatting step is now one function,
  `_format_proxy_cmd_args()`, called from both. `_scraper_proxy_args()`
  keeps its existing signature (selection + formatting) for its test/smoke-
  script callers. New test asserts the two paths produce identical flags
  for the same session key.
- 📄 **Provenance-fields guidance moved from Open work to Settled decisions**
  (was `CLAUDE.md` open work #2). `scripts/analysis/market_overlap.py`
  already implemented both halves of it — provenance-first with a
  `MIN(raw_leads.scrape_run_id)` fallback for NULL businesses — so it was
  describing current behavior, not a pending task. The still-open half (the
  backfill that would let cohort queries drop the fallback) stays in Open
  work, renumbered to #2.

## 2026-08-05

- ✨ **Block detection, proxy cooldown, sticky proxies, and pacing.** Neither
  this wrapper nor upstream `gosom/google-maps-scraper` had any block
  detection: a soft-blocked run came back with few or zero leads and was
  recorded as a normal `completed`, so the next run reused the same burned
  proxies. Four new pieces, all opt-out via env:
  - `app/scraper/block_detect.py` infers a block from yield — zero leads on a
    proxied run, or under `BLOCK_DETECT_LOW_YIELD_RATIO` (0.25) of the median
    of the last `BLOCK_DETECT_MIN_HISTORY` (3) `completed` runs for the same
    query+location. New `scrape_runs.status` value `blocked`; blocked runs are
    excluded from the baseline so consecutive blocks stay visible. Leads are
    still ingested and `_assess_run_health()` never raises.
  - `app/scraper/proxy_health.py` keeps a strike/cooldown ledger in
    `data/proxy_health.json` (gitignored, passwords never written). First
    strike parks a proxy for `PROXY_COOLDOWN_SEC` (600s), the second for
    `PROXY_RETIRE_SEC` (24h); a healthy run decays one strike. A fully-parked
    pool waits out the shortest cooldown (`PROXY_WAIT_MAX_SEC`, 900s) and
    retries, and only raises `ProxyPoolExhausted` if that can't help — a hard
    failure there would kill every remaining row of a ZIP batch.
  - Sticky proxy assignment replaces `random.shuffle` in
    `_select_scraper_proxies()`: a `blake2b` hash of the session key (query by
    default) picks a stable rotation offset, so the same variant keeps the same
    proxies across separate processes. `hash()` would not work — it is salted
    per process.
  - `app/scraper/pacing.py` adds jittered sleeps *between* scraper
    invocations via `SCRAPER_PACING_SEC="MIN:MAX"` (off unless set): depth
    iterations, full-harvest Pass 2 variants and Pass 3 ZIPs, and
    `run_zip_batch.py` rows. Never before the first invocation.
- 🐛 **README scraper-tuning guidance corrected.** The "3 proxies × 1 tab"
  recipe pinned `--scraper-browser-pool-size 1`, which in JS mode binds the
  whole pass to a *single* proxy (`jshttp.go:344-345` binds per browser
  context). Leave the pool unset so `scrapemate`'s
  `derivedBrowserPoolSize() = ceil(concurrency / pagesPerBrowser)` derives 3.
  Adds a strategy×mode table covering which knobs apply where and the
  per-request (fast mode) vs per-browser (JS mode) rotation difference.
- ✅ **Proxy-order test flakiness closed** (was `CLAUDE.md` open work #5).
  Scraper-side ordering is now deterministic by construction; crawler-side
  tests in `tests/test_extract_emails.py` assert set membership rather than a
  fixed order, leaving the crawler's deliberate shuffle intact. Suite: 249
  passed, deterministic across repeated runs.
- 🐛 **`--cell-km` is now validated on `run_pipeline.py`** — the check existed
  only in `run_zip_batch.py`, so `run_pipeline.py --grid --cell-km 0` was
  accepted and forwarded verbatim to the vendored scraper as `-grid-cell 0`,
  leaving the grid geometry up to the Go binary. `_validate_positive_counts()`
  now rejects non-positive values, before strategy dispatch so `--cell-km 0`
  errors even under single-centroid where the flag would otherwise be ignored.
  Adds the mirror-image scope warning: `--cell-km` under single-centroid now
  warns and is ignored, the same way `--min-contacts`/`--max-depth` do under
  grid/full-harvest.
- ♻️ **`DEFAULT_CELL_KM` moved to `run_pipeline.py`** — `run_zip_batch.py`
  defined its own copy while importing `DEFAULT_MAX_DEPTH` from
  `run_pipeline`. Grid cell size is a property of the grid, so it now follows
  the same convention. Comparing against the constant is how "did the operator
  pass `--cell-km`?" is inferred, so two copies could have diverged into a
  warning that never fires.
- 📄 **Docs consolidated** — `CLAUDE.md` 305 → ~250 lines. Sections restating
  `README.md` (crawl-attempt ledger, local Reacher, score map, venv setup)
  became pointers; the three overlapping strategy sections merged into one;
  hand-run SQL moved to a new `MAINTENANCE_SQL.md`; the crawl-provenance
  narrative left for this file. Dropped the `#R1`/`#12`/`#20`/`#22` prefixes —
  they referred to code-review rounds whose numbering no longer matches — and
  removed `robots.txt` from Open work, where it duplicated its own entry under
  Intentional deferrals.

## 2026-08-04

- 🐛 **Crawl-discovered contacts now carry provenance** —
  `_persist_emails_for_business()` in `app/pipeline/extract_emails.py` omitted
  `first_scrape_run_id` entirely, so every email found by website crawling landed
  with NULL provenance. Only `process_leads.py` ever stamped the field. Effect:
  contact-level lift tables built on `contacts.first_scrape_run_id` silently
  undercounted exactly the crawl-sourced emails that matter most — 25 of 163
  net-new HVAC contacts and 22 of 86 plumbing contacts, all of them with emails.
  `harvest_emails_from_websites()` now resolves `MAX(scrape_runs.id)` at harvest
  start and threads it through. Data written before this date still carries the
  gap; a backfill query is in `MAINTENANCE_SQL.md`.
- ✅ **Provenance tests fixed and extended** —
  `TestProcessLeadsProvenance::test_records_first_scrape_run_on_new_business_and_contact`
  had been failing on two unrelated bugs of its own: it referenced
  `process_leads.sessionmaker`, which does not exist (the import is
  function-local), and it read `run.id` after `session.close()`, raising
  `DetachedInstanceError`. Both fixed, and
  `test_records_first_scrape_run_on_crawl_discovered_contact` added as a
  regression guard on the crawl path. Suite: 5 pre-existing flaky proxy-order
  failures remain, down from 7.
- ✅ **`scripts/analysis/` added; root-level analysis scripts retired** —
  `market_overlap.py` (cohort overlap/lift), `export_cohort.py` (cohort-scoped
  CSV export), `run_wallclock.py` (interval-merged cohort duration). Replaces
  root-level `calculate_overlap.py` and `get_runtimes.py`, which both imported
  **pandas — not a declared dependency** — and so could not run inside the pinned
  `.venv`. The new scripts are stdlib + SQLAlchemy only.
  `market_overlap.py` also drops `place_id` matching: the pipeline deduplicates on
  base domain then `business_name` + E164 phone, never on `place_id`, so
  place_id-based matching could group raw leads differently from the pipeline.
- ✅ **Market overlap measured: San Jose ↔ Sunnyvale/Santa Clara** — 10.8%
  business overlap for plumbing (7 ZIPs), 12.6% for HVAC (3 ZIPs, after
  correcting for cross-vertical contamination; 19.6% uncorrected). The adjacent
  market is ~87–89% net-new. Results, caveats, and a continuation runbook are in
  `RUNS.md`.
- 🧹 **Mislabeled export quarantined** — `data/hvac_overlap_test_{all,deduped}.csv`
  were not HVAC and not a cohort: they were whole-DB exports (76/166 rows San
  Jose, mostly *plumbing*) produced because `export_new_leads()` has no
  run-cohort filter. Moved to
  `data/archive/MISLABELED_wholedb_export_2026-08-04_*.csv` with a README
  explaining the root cause.
- 🧹 **Interrupted runs marked** — 6 rows hard-killed by the 2026-08-04 network
  failure were sitting at `status = 'running'` (HVAC 56, 62, 63, 68; plumbing 22,
  42) and now read `interrupted`. Not a code bug: the exception handler in
  `execute_scrape_and_ingest()` does set `failed`, but SIGKILL never reaches it.
- ✅ **Per-city `_final` exports for Sunnyvale / Santa Clara** — four outreach
  files, one per city × vertical: Santa Clara HVAC 26, Sunnyvale HVAC 11, Santa
  Clara plumbing 25, Sunnyvale plumbing 12 (74 rows). **A floor, not a final
  count** — the crawl is unfinished on both DBs. Counts reconcile exactly against
  the cohorts: HVAC 37 kept + 14 junk + 12 out-of-city = 63 emailed contacts;
  plumbing 37 + 5 + 1 = 43.
- ✅ **`export_cohort.py` gained `--city` and `--drop-junk`** — `--city` resolves
  a business by its own address, falling back to the discovering run's ZIP when
  the address is blank (45 of 106 emailed cohort contacts sit on blank-address
  businesses, so an address-only filter would drop them), and prints the cities it
  excluded so a filter never silently discards leads. `--drop-junk` imports the
  crawler's own exclusion lists rather than copying them, so export cleanup cannot
  drift from crawl-time filtering.
- 🐛 **Three new email-filter exclusions**, all found in rows already exported:
  - `EXCLUDE_LOCALPARTS` added, holding `impallari` — a font designer's address
    embedded in webfont license headers, harvested from any site using his
    fonts. It shipped as a Santa Clara HVAC lead. `EXCLUDE_DOMAINS` cannot
    catch it: the address is `@gmail.com`, and blocking Gmail would drop most
    owner-operator contractors. The foundry-domain equivalents
    (`astigmatic.com`, `latofonts.com`) were already listed.
  - `xxx.xxx` — the all-x placeholder, shipped as a COOLMAN HVAC SUPPLY lead.
  - `address.com` — theme boilerplate `email@address.com`. The existing
    `email.com` entry misses it because matching is substring-based and stops
    at the `@`.
  - `eliteonlinemedia.com` — a web-agency contact-form relay, found on two
    unrelated plumbing sites. The same address on multiple businesses is the tell.
  - Three regression tests added in `tests/test_extract_emails.py`.
- 🧹 **ZIP files tidied** — `single_zip.csv` (untracked 1-ZIP scratch file)
  removed in favor of `zips_hvac_remaining_2026-08-04.csv` and
  `zips_plumbing_remaining_2026-08-04.csv`, which encode the ZIPs still owed work.

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
