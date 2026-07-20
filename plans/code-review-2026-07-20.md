# Code Review — 2026-07-20 (xhigh)

**Branch:** `lewis-test`
**Scope:** `git diff main...HEAD` — 25 files
**Method:** Workflow-backed review, xhigh effort. 6 finders → 44 candidates → 38 verified → 15 kept.

## Headline

Two blocking bugs in new `run_zip_batch` entrypoint:

1. Schema never bootstraps on fresh installs.
2. Any single-location failure aborts the whole batch and skips export.

Plus placeholder-delete FK hazard, per-host politeness on error branches, proxy-config validation gaps, cross-module inconsistencies, and two latent NULL/regex traps.

---

## Findings

### 🔴 CORRECTNESS — Blocking

#### 1. `run_zip_batch.py:83` — No `init_db()` call &nbsp;·&nbsp; **CONFIRMED**

**Failure:** Fresh install runs `python run_zip_batch.py --query ... --zip-file ...`. `run_location_pipeline` immediately calls `get_exportable_contact_count()` at `run_pipeline.py:81`. `run_pipeline.py:204` calls `init_db()` for the single-location path; batch runner doesn't. SQLAlchemy raises `OperationalError('no such table: contacts')` and the batch aborts before scraping.

**Fix:**
```python
# run_zip_batch.py, top of main()
from app.db.create_tables import init_db
init_db()
```

---

#### 2. `run_pipeline.py:68` + `run_zip_batch.py:106` — No per-zip try/except, export skipped on any failure &nbsp;·&nbsp; **CONFIRMED**

**Failure:** Batch running 20 zips. Zip 5 scraper subprocess times out (`SCRAPER_TIMEOUT_SEC` hits) → `subprocess.TimeoutExpired` unwinds out of `run_location_pipeline` → propagates through `main()`'s bare `for` loop → `export_new_leads()` at line 106 never runs. Leads for zips 1-4 sit in DB unexported. User can't tell from traceback which zips succeeded, must rerun batch (wastes scraper quota re-covering completed zips) or manually invoke `python -m app.pipeline.export_sheets`.

**Fix:**
```python
for i, location in enumerate(locations, 1):
    try:
        metrics = run_location_pipeline(...)
    except Exception as e:
        logger.exception("Location %d/%d failed: %s", i, len(locations), location)
        continue
# export runs after loop, unconditionally
export_new_leads()
```

---

#### 3. `app/pipeline/extract_emails.py:248` — `session.delete(placeholder)` violates ExportHistory FK &nbsp;·&nbsp; **CONFIRMED**

**Failure:** `_persist_emails_for_business` finds a new email for a business that previously had a phone-only placeholder Contact, and that placeholder was already exported (project keeps phone-only export per CLAUDE.md #12). ExportHistory holds a row with `contact_id` pointing to the placeholder. `session.delete(placeholder)`:

- **Postgres:** raises `ConstraintViolation` on commit → outer try/except rolls back → pipeline dies mid-run, no emails persisted.
- **SQLite** (FKs off by default): silently orphans the export_history row → poisons future NOT IN filters (see #14).

**Fix:** either `ondelete="CASCADE"` on `ExportHistory.contact_id`, or delete matching ExportHistory rows first, or convert placeholder in place (update its `email` column) instead of delete-then-insert.

---

#### 4. `app/scraper/run_scraper.py:79` — Trailing comma in `SCRAPER_PROXIES` kills run &nbsp;·&nbsp; **CONFIRMED**

**Failure:** Operator sets `SCRAPER_PROXIES='http://p1:8080,http://p2:8080,'` (trailing comma is easy to leave in copy-paste). `raw_proxies.split(',')` yields `['http://p1:8080', 'http://p2:8080', '']`. Empty string flows into `_validate_proxy_url` → `ValueError('Proxy URL cannot be empty.')` → `execute_scrape_and_ingest` marks ScrapeRun failed and re-raises → `sys.exit(1)`. Pipeline dies at startup on easy config mistake.

**Fix:**
```python
raw = os.getenv("SCRAPER_PROXIES", "")
proxies = [p.strip() for p in raw.split(",") if p.strip()]
```

---

#### 5. `app/pipeline/extract_emails.py:116` — Crawler rejects `socks5`, scraper accepts it &nbsp;·&nbsp; **CONFIRMED**

**Failure:** Two parallel `_validate_proxy_url` helpers with silently-different accept sets. `run_scraper.py:33` accepts `{http, https, socks5, socks5h}`; `extract_emails.py:116` accepts `{http, https}` only. Operator standardizes on `CRAWLER_PROXY=socks5://proxy:1080` (verified against scraper) → crawler raises `ValueError('Unsupported crawler proxy scheme socks5. Allowed: http, https')`. Also leaks session (see #7).

**Fix:** align the two allowlists. Simplest — keep crawler at `{http, https}`, remove `socks5*` from scraper unless there's a real need. Or extract a shared helper in `app/util/proxy.py`.

---

#### 6. `app/pipeline/verify_emails.py:132` — NOT IN with nullable subquery silently returns zero &nbsp;·&nbsp; **CONFIRMED**

**Failure:** `~Contact.id.in_(session.query(EmailVerification.contact_id))`. `EmailVerification.contact_id` is nullable (no `nullable=False`). SQL three-valued logic: a single row with `contact_id=NULL` makes NOT IN return UNKNOWN for every row → "unverified contacts" query yields zero → verifier logs `"Verifying 0 unverified contacts via Reacher"` → commits nothing → exits successfully, silently skipping every contact.

**Fix:**
```python
subq = session.query(EmailVerification.contact_id).filter(
    EmailVerification.contact_id.isnot(None)
)
# or use NOT EXISTS
```

---

#### 7. `app/pipeline/extract_emails.py:259` — Session leak on proxy config error &nbsp;·&nbsp; **CONFIRMED**

**Failure:** `session = Session()` (line 259), then `crawler_proxies = _build_crawler_proxies()` (line 260) BEFORE the `try:` block (line 262). Bad env var (`CRAWLER_PROXY=proxy.example.com` missing scheme, or `CRAWLER_PROXY_FILE=/does/not/exist`) raises → `finally: session.close()` at line 321 never runs. DB connection leaks and operator sees raw traceback instead of friendly error log.

**Fix:** move `_build_crawler_proxies()` above `Session()`, or inside the try/finally.

---

#### 8. `app/pipeline/extract_emails.py:207` — Early `break` skips per-host politeness sleep &nbsp;·&nbsp; **CONFIRMED**

**Failure:** Email found on `/contact` or `/contact-us` → `break` at line 207 exits the for-loop BEFORE `time.sleep(PER_HOST_DELAY_SEC)` at line 209. Per-host lock releases with zero delay. Multiple businesses sharing a host (chains, Yelp/Facebook, missed dedupes) → next thread hits server the same instant → 429/CAPTCHA/blocklist risk → zero emails harvested for rest of host.

**Fix:** move sleep into a `finally` inside the loop iteration, or sleep before releasing lock.

---

### 🟡 CORRECTNESS — Moderate

#### 9. `run_zip_batch.py:24` — `_row_location` requires zip; city+state rows silently skipped &nbsp;·&nbsp; **CONFIRMED**

**Failure:** All three fallback branches guard on `zip_code`. CSV with just `city,state` (no zip column, or neighborhoods without zips) → every row returns `None` → `load_locations` warns "Skipping row N" for every entry → `ValueError('Zip file contained no usable rows.')`. Batch aborts even though `f"{city}, {state}"` is a valid Nominatim query.

**Fix:** add fallback:
```python
if city and state:
    return f"{city}, {state}"
```

---

#### 10. `run_zip_batch.py:35` — Excel BOM breaks first fieldname &nbsp;·&nbsp; **CONFIRMED**

**Failure:** `encoding="utf-8"` doesn't strip Excel's `0xEF 0xBB 0xBF` BOM. `csv.DictReader` reads the first fieldname as `"﻿zip"`. `row.get("zip")` returns None on every row → same "no usable rows" ValueError, misleading error message. Excel writes BOM by default when saving as "CSV UTF-8".

**Fix:** `encoding="utf-8-sig"`.

---

#### 11. `app/logging_config.py:25` — `_CONFIGURED` guard runs BEFORE checking passed `level` &nbsp;·&nbsp; **CONFIRMED**

**Failure:** REPL/notebook/test harness first calls `setup_logging()` (INFO default), then later `setup_logging(level='DEBUG')` to raise verbosity for targeted rerun. Second call hits `if _CONFIGURED: return` and exits before touching the level. No error, no warning — DEBUG output silently dropped. Looks like the code never hit those log lines.

**Fix:** either move the guard below level resolution, or drop the guard entirely and let `root.setLevel(numeric_level)` idempotently update.

---

#### 12. `app/logging_config.py:22` — Module `__main__` blocks never call `setup_logging()` &nbsp;·&nbsp; **CONFIRMED** (cleanup)

**Failure:** Only `run_end_to_end_pipeline` and `run_zip_batch.main` invoke `setup_logging()`. Every other module with a `__main__` guard (`verify_emails.py`, `extract_emails.py`, `process_leads.py`, `export_sheets.py`, `run_scraper.py`) never configures the root logger. Operator runs the CLAUDE.md-documented manual verifier — `python -m app.pipeline.verify_emails` — and sees a blank terminal for the entire batch. INFO messages dropped by default root logger. Indistinguishable from a hang.

**Fix:** add `setup_logging()` to each `if __name__ == "__main__":` block.

---

#### 13. `run_pipeline.py:68` + `app/pipeline/export_sheets.py:131` — Destination-literal mismatch &nbsp;·&nbsp; **CONFIRMED**

**Failure:** `run_location_pipeline` filters `ExportHistory` on `LEGACY_EXPORT_DESTINATION="local_csv_leads"`. `export_new_leads` writes `destination=SPREADSHEET_ID` when Sheets is configured. With a real Sheets ID, `get_exportable_contact_count` always counts the full Contact table (nothing was ever tagged `"local_csv_leads"`). Baseline and per-iteration values both equal total row count. Delta arithmetic happens to still work within one process — but the metric name lies to operators and the gate silently drifts if any stale local-CSV entry exists or a caller passes a non-default destination.

**Fix:** thread the active export destination through `run_location_pipeline` → `get_exportable_contact_count`. Don't hardcode `LEGACY_EXPORT_DESTINATION`.

---

### 🟠 LATENT

#### 14. `run_pipeline.py:57` — NOT IN with nullable `ExportHistory.contact_id` &nbsp;·&nbsp; **PLAUSIBLE**

**Failure:** Same NULL trap as #6, at a different call site. `ExportHistory.contact_id` is a nullable FK (`create_tables.py:137`). Single NULL row for the matching destination → NOT IN returns UNKNOWN for every row → `get_exportable_contact_count` returns 0. `run_location_pipeline` collapses baseline and per-iteration to 0 → `new_exportable_contacts` stays 0 → `stale_iterations` trips → batch quietly stops each zip without scraping deeper pages. NULL rows can appear after the placeholder-delete orphan path in #3 (SQLite case), or after any manual data fix.

**Fix:** same as #6 — filter subquery with `.isnot(None)`, or migrate schema to `nullable=False` (needs migration for existing rows).

---

#### 15. `app/pipeline/process_leads.py:54` — Regex switch changes historical dedup &nbsp;·&nbsp; **PLAUSIBLE**

**Failure:** Diff replaced `startswith(('http://', 'https://'))` with `re.match(r'^https?://', url_str, re.IGNORECASE)`. Old code was case-sensitive: mixed-case URLs like `HTTPS://example.com` fell through the check, got `http://` prepended, then produced the garbage domain `'http:'`. Databases built under the old code have Business rows with `domain='http:'`. After this patch, a fresh scrape of the same business now yields the correct domain (e.g., `example.com`) → `process_and_deduplicate_leads` treats it as new → duplicate Business row alongside historical `'http:'` one → contacts split, export CSV shows same business twice.

**Fix:** one-shot cleanup SQL before next run:
```sql
-- inspect first
SELECT id, name, domain FROM businesses WHERE domain = 'http:' OR domain LIKE 'http:%';
-- merge or delete garbage rows, then reprocess raw_leads with processed_at cleared
```

---

## Refuted (dropped)

| File:Line | Claim | Why dropped |
|-----------|-------|-------------|
| `run_scraper.py:79` | Comma-in-credentials silently corrupts proxy URLs | `_validate_proxy_url` catches malformed fragments |
| `extract_emails.py:152` | CRAWLER_PROXY_FILE only-first-line is a bug | Intentional per `features/proxy-support-plan.md:41-53` |
| `create_tables.py:164` | File missing trailing newline | Pure style, no runtime effect |
| `run_zip_batch.py:85` | `setup_logging()` runs after `load_locations()` | Wrong — runs BEFORE, on line 85 vs 87 |
| `test_extract_emails.py:104` | Test hardcodes bug | Test certifies intentional design per plan doc |
| `run_scraper.py:79` | Comma-in-credential proxy URLs silently corrupt | Validator enforces scheme/host/port |

---

## Commit sequencing (recommended)

**Round 1 — unblock batch runner:**
1. #1 init_db in run_zip_batch
2. #2 per-zip try/except + unconditional export
3. #4 filter empty proxy strings
4. #10 utf-8-sig for CSV

**Round 2 — correctness in crawler:**
5. #3 fix placeholder-delete FK path (also unblocks #14 SQLite orphan)
6. #7 move `_build_crawler_proxies()` inside try/finally
7. #8 sleep in finally for per-host politeness

**Round 3 — cross-module consistency:**
8. #5 align proxy scheme allowlists
9. #6 filter NULL from verify_emails NOT IN
10. #11 fix logging guard vs level arg
11. #12 add setup_logging to __main__ blocks
12. #13 thread destination through run_location_pipeline

**Round 4 — schema/data hygiene:**
13. #14 filter NULL from get_exportable_contact_count NOT IN (+ schema `nullable=False` migration)
14. #15 clean up `domain='http:'` legacy rows

---

**Review stats:** 6 finders, 44 candidates, 38 verified, 15 reported, 6 refuted. 4.66M subagent tokens, 47 agents, ~74 min wall time.
