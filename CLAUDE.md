# email-scraper-verifer-cleaner — Review Notes

Living review + backlog. Updated 2026-07-19.

Purpose: HVAC/Plumbing lead-gen pipeline. Scrapes Google Maps → SQL →
dedupes → crawls sites for emails → optionally verifies via self-hosted
Reacher on Kamatera → exports Sheets/CSV.

---

## Pipeline flow (as-built)

```
run_pipeline.run_end_to_end_pipeline(query, location, min_contacts)
  init_db()                              # bootstrap schema once
  lat, lon = geocode_location(location)  # ONCE via Nominatim, cached
  loop (depth 1 → max 20, step +2):
    run_scraper.execute_scrape_and_ingest(query, location, lat, lon)  # subprocess → raw_leads
    process_leads.process_and_deduplicate  # unprocessed raw_leads → businesses + contacts
    extract_emails.harvest_emails_from_websites  # concurrent crawl /contact,/about,... → contacts
    if contact_count >= min_contacts: break
  export_sheets.export_new_leads         # Sheets → fallback CSV
```

Verification (`app/pipeline/verify_emails.py`) is NOT auto-wired into the
loop. Runs manually via `python -m app.pipeline.verify_emails` or
imported into `run_pipeline` on demand. Archived predecessor:
`verify_emails_ARCHIVE.py` (BillionVerify — kept for reference).

---

## Recently closed

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

### #12 — Export pushes empty-email rows to Sheets *(deferred by request)*

`export_sheets.py:129` — no `Contact.email IS NOT NULL` filter, so
phone-only placeholder contacts (`email = NULL`) get exported with a
blank Email column. Left as-is per project decision.

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
   Probably wants "new this run".

4. **`max_depth=20`** — no evidence 20 is a rational upper bound. What
   does depth=20 mean to the Go scraper (results count? radius?
   Playwright pages loaded?). Needs research.

5. **Verifier exists but is not wired into `run_pipeline`** — deliberate
   as of now. `app/pipeline/verify_emails.py` is implemented and runnable
   manually, but `run_pipeline.py` does not call it inside scrape /
   process / harvest loop or before export. If we want inline
   verification later, decide when it should run (each iteration vs once
   at end) and whether cost / latency is acceptable.
