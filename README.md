# Email Scraper, Verifier & Cleaner (HVAC / Plumbing Lead Engine)

Local-first, cloud-ready lead-gen pipeline. Finds, cleans, verifies, and
exports business leads (HVAC / plumbing) from Google Maps + target websites.

---

## Pipeline

```
Google Maps Scraper (Go binary)
        ↓
Raw Leads (SQLite / Postgres)
        ↓
Clean + Normalize + Dedupe (domain, name+phone)
        ↓
Email Harvest (crawl /contact, /about, /team; regex extract)
        ↓
Email Verify (Reacher on Kamatera)   [optional, wire in verify_contacts_emails]
        ↓
Google Sheets / local CSV export (with export-history dedupe)
```

The verify step is currently **not** called from `run_pipeline.py`. Wire it
in explicitly when you want it — see [Verification](#verification) below.

---

## Setup

### 1. Prerequisites

- Python 3.10+
- macOS or Linux (Windows works too, use the `.exe` binary variant)
- Google Maps scraper binary (see below)

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Scraper binary

The project shells out to `gosom/google-maps-scraper`. Drop the binary
into `app/scraper/`:

| OS               | Filename                          |
| ---------------- | --------------------------------- |
| macOS / Linux    | `app/scraper/google-maps-scraper` |
| Windows          | `app/scraper/google-maps-scraper.exe` |

Selection is OS-aware — `run_scraper.py` picks the right one at runtime
and raises `FileNotFoundError` with an actionable message if neither is
present. `chmod +x` the unix binary after downloading.

Build from source:

```bash
git clone https://github.com/gosom/google-maps-scraper.git
cd google-maps-scraper
go build -o google-maps-scraper .
mv google-maps-scraper /path/to/repo/app/scraper/
chmod +x /path/to/repo/app/scraper/google-maps-scraper
```

### 4. `.env`

```env
# Database — SQLite for local, Postgres for cloud
DATABASE_URL=sqlite:///database/hvac_leads.db
# DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/leads

# Google Sheets export — set SPREADSHEET_ID to 'mock' to fall through to CSV
SPREADSHEET_ID=mock
CREDENTIALS_FILE=credentials.json

# Reacher email verifier (self-hosted on Kamatera; see below)
REACHER_API_URL=http://104.128.66.74:8080/v0/check_email
REACHER_TIMEOUT_SEC=30

# Optional proxy config
SCRAPER_PROXIES=http://user:pass@proxy1.example.com:8080,socks5://proxy2.example.com:1080
SCRAPER_PROXIES_FILE=proxies.txt
CRAWLER_PROXY=http://user:pass@proxy3.example.com:8080
CRAWLER_PROXY_FILE=proxies.txt
# Or split crawler proxies by scheme
# CRAWLER_HTTP_PROXY=http://proxy-http.example.com:8080
# CRAWLER_HTTPS_PROXY=https://proxy-https.example.com:8443

# Kamatera deploy credentials (only needed if you re-provision the
# verifier server — the verify_emails.py module itself does NOT need them)
KAMATERA_ACCESS_KEY=...
KAMATERA_SECRET_KEY=...
```

`database.py` raises a clear error if `DATABASE_URL` is unset.

---

## Usage

### Run the full pipeline

```bash
python run_pipeline.py --query "Plumbing" --location "San Francisco, CA"
```

Defaults:
- `--min-contacts 500`
- `--max-depth 20`

### Override campaign settings

```bash
python run_pipeline.py \
  --query "HVAC" \
  --location "Plano, TX" \
  --min-contacts 50 \
  --max-depth 9
```

`run_pipeline.py` keeps legacy semantics: `--min-contacts` is total DB
contacts, not new contacts from current run.

### Batch zip-file mode

Use `run_zip_batch.py` when you want one CSV of zips/locations and a per-zip
success target.

```bash
python run_zip_batch.py \
  --query "Plumbing" \
  --zip-file san_jose_zips.csv \
  --target-new-exportable 20 \
  --max-depth 9
```

CSV formats supported:

```csv
zip
95112
95123
```

```csv
zip,city,state
95112,San Jose,CA
95123,San Jose,CA
```

```csv
location
San Jose, CA 95112
San Jose, CA 95123
```

Batch semantics:
- `--target-new-exportable` = new contacts from this zip not yet exported
- stops each zip on target reached, `--max-depth`, or stale iterations
- exports once at batch end

The scraper depth starts at 1 and grows by 2 each iteration up to
`--max-depth`. The location is geocoded **once** at pipeline start and passed
into every subsequent scrape iteration — Nominatim ToS friendly.

Proxy notes:
- `SCRAPER_PROXIES` passes comma-separated proxies straight to gosom `-proxies`.
- `SCRAPER_PROXIES_FILE` loads one proxy URL per line and appends them to `SCRAPER_PROXIES`.
- `CRAWLER_PROXY` applies one HTTP/HTTPS proxy to website crawling.
- `CRAWLER_PROXY_FILE` loads one proxy URL per line; crawler uses first valid entry.
- `CRAWLER_HTTP_PROXY` and `CRAWLER_HTTPS_PROXY` override `CRAWLER_PROXY` / `CRAWLER_PROXY_FILE` per scheme.
- Crawler proxy support accepts `http`, `https`, `socks5`, and `socks5h`.

---

## Verification

Email verification is done against a self-hosted [Reacher
`check-if-email-exists`](https://github.com/reacherhq/check-if-email-exists)
backend deployed to a Kamatera server (Hetzner blocks outbound SMTP port
25 for new accounts; Kamatera does not). Live instance:

- URL: `http://104.128.66.74:8080/v0/check_email`
- Deploy scripts, keys, and infra config live in the sibling repo
  `autopilotlocal/email-verifier`.

To run verification against your current contacts:

```bash
python -m app.pipeline.verify_emails
```

This POSTs each unverified contact email to Reacher, persists an
`EmailVerification` row, and updates `Contact.lead_status`:

| `is_reachable` | `lead_status` | derived score |
| -------------- | ------------- | ------------- |
| `safe`         | Verified      | 95            |
| `risky`        | Risky         | 50            |
| `invalid`      | Invalid       | 10            |
| `unknown`      | Unknown       | 25            |

The old BillionVerify-based implementation is archived at
`app/pipeline/verify_emails_ARCHIVE.py` — retained for reference, not
imported anywhere.

To wire verification into the main pipeline, import
`verify_contacts_emails` from `app.pipeline.verify_emails` in
`run_pipeline.py` and call it between `harvest_emails_from_websites()`
and `export_new_leads()`.

---

## Logging

Every module logs via `app.logging_config.get_logger(__name__)`. Level is
controlled by the `LOG_LEVEL` env var (default `INFO`; set `DEBUG` for
verbose runs). Format:

```
2026-07-19 19:30:42 [INFO] app.pipeline.extract_emails: Checking 42 businesses...
```

`urllib3` and `requests` are pinned to `WARNING` so their per-request
noise doesn't drown out pipeline output. Add a `FileHandler` to
`app/logging_config.py` if you need on-disk logs.

---

## Tests

```bash
.venv/bin/pytest

# Live proxy smoke test (uses .env + CRAWLER_PROXY_FILE / CRAWLER_PROXY)
.venv/bin/python scripts/smoke_test_proxies.py
```

The suite covers pure helpers plus orchestration/proxy edge cases —
`extract_domain`, `normalize_phone`, `_parse_and_validate_emails`,
`extract_emails_from_html`, Reacher response handling in
`verify_email_via_reacher` (mocked, no live server needed), scraper proxy
parsing, and `run_pipeline` / `run_zip_batch` control-flow behavior such
as batch init and continue-on-error handling. DB-heavy
`process_and_deduplicate_leads` and live network crawling in
`harvest_emails_from_websites` are still integration-test territory and
are intentionally not covered here.

`tests/conftest.py` sets `DATABASE_URL=sqlite:///:memory:` before any
`app.db.database` import so tests never touch a real DB.

---

## Project structure

```
├── app/
│   ├── logging_config.py       # Central logging setup
│   ├── db/
│   │   ├── create_tables.py    # SQLAlchemy models + init_db()
│   │   └── database.py         # Engine + DATABASE_URL guard
│   ├── pipeline/
│   │   ├── export_sheets.py    # Google Sheets + CSV export
│   │   ├── extract_emails.py   # Concurrent multi-path website crawler
│   │   ├── process_leads.py    # Clean + dedupe (batch preloaded)
│   │   ├── verify_emails.py    # Reacher API client (active)
│   │   └── verify_emails_ARCHIVE.py  # BillionVerify version, archived
│   └── scraper/
│       ├── google-maps-scraper[.exe]  # Compiled Go binary (gitignored)
│       └── run_scraper.py      # Subprocess wrapper + geocoder
├── tests/                      # Pytest suite for pure helpers
├── data/leads_export.csv       # Fallback CSV export (gitignored)
├── database/hvac_leads.db      # SQLite (gitignored)
├── .env                        # Local config (gitignored)
├── requirements.txt
├── run_pipeline.py             # Orchestrator entrypoint
├── CLAUDE.md                   # Review notes + backlog
└── README.md
```

---

## Database schema

- **`scrape_runs`** — one row per scraper invocation.
- **`raw_leads`** — scraper output, tagged `processed_at` after promotion.
- **`businesses`** — canonical deduped businesses, `domain` UNIQUE.
- **`contacts`** — one row per person/inbox; `(business_id, email)` UNIQUE.
- **`email_verifications`** — Reacher results, one per contact.
- **`export_history`** — every (`contact_id`, `destination`) push, with `exported_at` timestamp.

Indexes cover the FK columns + all `WHERE`-clause candidates
(`raw_leads.processed_at`, `contacts.lead_status`, etc.).

If you have an older DB, add/backfill `export_history.exported_at` before relying on that field in reporting or audits. Legacy DBs from before the case-insensitive URL normalization fix may also contain bad `businesses.domain` values like `http:` that need manual cleanup.
