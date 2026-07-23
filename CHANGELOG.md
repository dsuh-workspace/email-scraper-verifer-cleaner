# email-scraper-verifer-cleaner — Changelog

Closed review items and shipped fixes, archived out of `CLAUDE.md` to keep
that file focused on open work. Not auto-loaded into context — read on
demand.

## Recently closed

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
