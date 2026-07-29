# email-scraper-verifer-cleaner — Changelog

Closed review items and shipped fixes, archived out of `CLAUDE.md` to keep
that file focused on open work. Not auto-loaded into context — read on
demand.

## Recently closed

- ✅ **#R6 `run_zip_batch.py --strategy`** (2026-07-29) — the batch path was
  single-centroid-only, which made metro-wide grid/full-harvest (the actual
  coverage play) unreachable from the CSV runner. Rather than reimplement
  the passes, each strategy was extracted from `run_end_to_end_pipeline`
  into its own function in `run_pipeline.py` — `run_location_grid()` and
  `run_location_full_harvest()`, alongside the existing
  `run_location_pipeline()` — and both CLIs now call the same three.
  `run_end_to_end_pipeline` is geocode + dispatch + the verify/export tail;
  ~150 lines of strategy body left it, and all 166 pre-existing tests
  passed unchanged, which is the evidence the move was behavior-preserving.
  - All three return the same `LocationRunMetrics`, so the batch logs one
    line per row regardless of strategy. `_location_metrics()` snapshots DB
    counts for the two non-looping strategies; `stale_iterations=0` is by
    construction, not a fake — a fixed set of passes has no consecutive
    zero-yield depth bumps.
  - `_resolve_strategy` / `_resolve_query_variants` are **imported** by
    `run_zip_batch.py`, not copied. Both CLIs must agree on what `--grid`
    means and on when full-harvest is refused for lacking a variant set
    (exit 2) — that check is the difference between a real sweep and
    grid-level results at full wall cost.
  - New batch flags `--strategy` / `--grid` / `--cell-km` / `--queries`.
    The three depth-loop flags moved to `default=None` and are
    single-centroid only (warn + ignore elsewhere, same treatment
    `run_pipeline.py` already gave `--min-contacts`/`--max-depth`);
    non-positive values and `--cell-km <= 0` exit 2.
  - Batch full-harvest deliberately omits `zip_csv`: Pass 3 is a fast ZIP
    top-up and the batch already *is* the ZIP sweep. It warns once up front
    that every row costs a grid pass plus a multi-query centroid sweep.
  - Grid/full-harvest raise when Nominatim returns no bbox instead of
    degrading to a centroid scrape — a silent downgrade would report
    grid metrics for a single-centroid run. Caught per-row in the batch, so
    one unmappable ZIP doesn't end the sweep.
  - Tests: 15 new (`TestZipBatchStrategies` in `tests/test_run_pipeline.py`)
    covering dispatch per strategy, the `--grid` shorthand, the ignored-flag
    warnings, exit-2 validation, per-row geocoding, Pass 3 omission, and
    continue-on-error. Full suite: **181 passing**. Two pre-existing grid
    tests and `TestFullHarvestStrategy._wire` gained a
    `get_exportable_contact_count` stub — the baseline-delta metrics are new
    on those paths.
- ✅ **#R7 crawl-attempt ledger** (2026-07-29) — `harvest_emails_from_websites()`
  no longer re-crawls businesses that previously yielded no email. Two new
  columns on `businesses`: `last_crawled_at` (stamped on every attempt,
  including ones that error) and `crawl_attempts` (count of *consecutive*
  no-email attempts, reset to 0 on success so a site that starts publishing
  an address isn't pinned at the give-up threshold). The pending set is a
  three-way split — has-email / given-up / in-cooldown / crawl — and the log
  line reports all four buckets. Tunable via `CRAWL_RETRY_AFTER_HOURS`
  (default 720h; `0` and non-integers rejected back to the default, since a
  zero-hour cooldown restores the original bug) and `CRAWL_MAX_ATTEMPTS`
  (default 3; `0` legitimately means no cap). See CLAUDE.md
  "Crawl-attempt ledger" for the full contract.
  - Crawl errors deliberately count as spent attempts — a domain that
    reliably times out would otherwise be retried forever, which is the
    same failure mode the ledger exists to fix.
  - `init_db()` gained `_apply_additive_columns()`: `create_all()` creates
    missing tables but never alters existing ones, so an older DB file
    would raise "no such column" on the first harvest. Idempotent,
    additive-only. Verified against a copy of the live 95-row
    `hvac_leads.db` — columns added, rows preserved, no-op on re-run.
  - The crawl stays inside the depth loop. It has to: `--min-contacts`
    counts new *exportable* contacts, exportability requires an email, and
    an email requires the crawl — a loop gating on that count that doesn't
    crawl each iteration never advances and burns all `max_depth`
    iterations. The ledger makes staying in the loop cheap instead of
    moving the crawl out.
  - Tests: 14 new (28 → 42 in `tests/test_extract_emails.py`), including 7
    that run `harvest_emails_from_websites()` against a real throwaway
    SQLite file with the network stubbed. The regression guard for #R7 is
    `test_second_harvest_skips_email_less_businesses`. Full suite: 166
    passing under `.venv/bin/python -m pytest`.
- ✅ **`Contact`/`ExportHistory` NameError in `_persist_emails_for_business`**
  (2026-07-29) — **pre-existing live bug**, unrelated to #R7, found while
  verifying it. Both names were imported only inside
  `harvest_emails_from_websites()`; a function-local import does not enter a
  sibling function's scope, so `_persist_emails_for_business()` raised
  `NameError` on the **first crawl that actually found an email**. The
  harvest's `except` clause logged it and re-raised, so every successful
  crawl aborted the harvest. Fixed with a function-local import matching the
  module's existing deferred-import convention (module-level `app.db` import
  would fire `DATABASE_URL` validation on every worker import). Not caught
  earlier because no test exercised the persist path against a real DB — the
  new ledger tests do.
- ✅ **Architecture/doc alignment** (2026-07-28) — follow-up pass after the
  2026-07-23 review, per project decisions of the same date:
  - **One vertical per run.** `_default_harvest_queries()` returns `None`
    for a query naming both plumbing and HVAC; full-harvest hard-errors
    (exit 2) telling the operator to split the run or pass `--queries`.
    This replaces the combined 16-query union added earlier the same day
    (`DEFAULT_COMBINED_HARVEST_QUERIES` deleted) — it still fixes review
    #3's unreachable-HVAC-branch defect without doubling Pass 2 cost.
  - **Single-centroid delegates to `run_location_pipeline()`.** The
    duplicate depth loop in `run_end_to_end_pipeline` is gone; there is
    now one depth-loop implementation, shared with `run_zip_batch.py`,
    and one definition of "enough contacts". `run_location_pipeline`
    gained `lat`/`lon` params so the caller's already-resolved centroid
    is reused — still exactly one Nominatim call per run.
  - **`--min-contacts` = new exportable contacts produced by this run**,
    not cumulative DB contacts, so re-running against a populated DB
    still scrapes. Uses the existing
    `get_exportable_contact_count()`-minus-baseline delta. Single-centroid
    only; grid/full-harvest still warn.
  - **#16 CLI validation closed.** `_validate_positive_counts()` rejects
    non-positive `--min-contacts` / `--max-depth` via `parser.error()`
    (exit 2, with usage) instead of letting a `0` produce a loop that
    exits immediately or a negative depth reach the scraper.
  - **#R8 closed** — `scripts/harvest_best.py` moved to
    `scripts/experiments/harvest_best.py`, documented as offline-only
    (no DB, no dedupe, no crawl), with `run_pipeline.py --strategy
    full-harvest` named as the production equivalent. Its `parents[1]`
    path math was bumped to `parents[2]` for the deeper location —
    without that the `scripts.scrape_experiment` import and the
    `data/harvests/` output root would have resolved under `scripts/`.
  - **Docs corrected**: `--min-score` help and CLAUDE.md both said the
    Reacher map was safe=100/invalid=0; the actual `_SCORE_BY_STATUS` is
    **safe=95, risky=50, unknown=25, invalid=10**. README no longer says
    verification is unwired (it runs via `--verify`). CLAUDE.md's
    "`HVAC/Plumbing` hardcoded" claim was stale — `run_scraper.py:220`
    has been scraper-category-first-with-query-fallback since `393a10c`.
  - **#R7 re-scoped** — the original "move the crawl out of the depth loop"
    framing was wrong; the crawl cannot leave the loop (exportability
    requires an email, which requires the crawl). Re-scoped to the real
    waste — `extract_emails.py` re-crawling businesses that previously
    yielded no email — and closed the next day by the crawl-attempt ledger
    entry above.
  - **#R10 dropped** from the backlog — untracked working files are
    handled manually by the operator and are not tracked here.
- ✅ **Review 2026-07-23 items #1–#15** (2026-07-28) — 15 findings from the
  workflow-backed review in `plans/code-review-2026-07-23.md`:
  - **#2/#3/#4/#8/#12 `_default_harvest_queries` rewritten.** Keyword
    matching is now word-boundary regex (`_PLUMBING_PATTERNS` /
    `_HVAC_PATTERNS`), so `AC`/`A/C` classify as HVAC without `ac` matching
    inside `backflow`/`vacuum`. `leak` dropped from the plumbing set (an
    "AC leak repair" query is HVAC work). A query naming both industries
    no longer silently resolves to whichever branch was checked first, so
    the HVAC set is no longer unreachable for combined queries (it
    returns `None` → hard error; see the one-vertical-per-run entry
    above). Blank query raises `ValueError`. The helper is pure —
    it returns `None` for an unknown industry and no longer logs, so the
    caller owns the message.
  - **#1/#9/#11 full-harvest Pass 2 degradation is loud.** Pass 2's variant
    list resolves inside the centroid guard, so no warning fires for a pass
    that gets skipped. CLI hard-errors (exit 2) when full-harvest can't
    derive a variant set and no `--queries` was given. The library path
    keeps the warning and now repeats it in the "Full-harvest complete"
    line. `queries=()` raises instead of silently reverting to defaults —
    only `queries=None` means "derive from industry".
  - **#5/#6 flag-scope warnings symmetric.** `--min-contacts` /
    `--max-depth` default to `None` (`DEFAULT_MIN_CONTACTS` /
    `DEFAULT_MAX_DEPTH` applied inside the pipeline), so "user passed the
    flag" no longer means "value != 500". Both warn when combined with
    grid/full-harvest; both help texts say single-centroid only.
  - **#7 `--queries` with a non-full-harvest strategy is an error**, not a
    warning that scrolls past in cron output. `--bbox` / `--zip-csv` stay
    warnings pending a decision on the same treatment.
  - **#13 `min_contacts`/`max_depth` are `int | None`** on
    `run_end_to_end_pipeline`, and `main()` passes `None` for strategies
    that ignore them.
  - **#14 Pass 3 log lines unified** — success and geocode-failure both use
    the `[i/N zip Z]` prefix, so one parser regex covers both.
  - **#10 tests** cover the degraded-Pass-2 warning, the CLI hard errors,
    the `--max-depth` warning, and the classifier edge cases.
- ✅ **#R0 `run_location_pipeline` NameError** (fixed by commit `d8df29f`
  "Add scraper tuning controls", logged 2026-07-28). The release blocker:
  `run_location_pipeline` passed `concurrency=scraper_concurrency`,
  `browser_pool_size=scraper_browser_pool_size`,
  `pages_per_browser=scraper_pages_per_browser`, and
  `proxy_limit=scraper_proxy_limit` into `execute_scrape_and_ingest()` with
  none of those names in scope → `NameError` on every single-centroid run
  and every `run_zip_batch.py` path. Closed via fix option 1 (add the
  matching kwargs to the signature and thread them through from callers),
  not by reverting. The entry was dropped from `CLAUDE.md` in `986b640`
  without a CHANGELOG counterpart; recorded here for traceability
  (review 2026-07-23 #15).
- ✅ **Review 393a10c items R2/R3/R4/R5** (2026-07-22):
  - **#R2** `main()` now only builds `query_variants` when
    `strategy == "full-harvest"`. Warning message kept for user feedback.
  - **#R3** Full-harvest Pass 3 logs `[i/N] scraping <zip_loc>` before
    each per-ZIP scrape call.
  - **#R4** Grid + full-harvest strategy completion logs drop the
    misleading `(target %d)` clause. `main()` warns when
    `--min-contacts` is non-default and strategy ≠ single-centroid.
    `--min-contacts` help text now says "single-centroid only".
  - **#R5** New `_default_harvest_queries(query)` helper picks
    plumbing/HVAC set by keyword; unknown industries get
    `(query,)` + a warning telling the user to pass `--queries`.
    Added `DEFAULT_HVAC_HARVEST_QUERIES` set. Tests cover HVAC,
    plumbing, and unknown-fallback paths.
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
