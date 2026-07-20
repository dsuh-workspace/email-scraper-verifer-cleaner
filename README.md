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
python run_pipeline.py
```

### Change the search target

Edit the `__main__` block in `run_pipeline.py`:

```python
if __name__ == "__main__":
    run_end_to_end_pipeline(
        query="Plumbing",              # industry keyword
        location="San Francisco, CA",  # geocoded once via Nominatim, cached
        min_contacts=500,              # loops scraper depth until DB has this many contacts
    )
```

The scraper depth starts at 1 and grows by 2 each iteration up to 20. The
location is geocoded **once** at pipeline start and passed into every
subsequent scrape iteration — Nominatim ToS friendly.

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
```

The suite covers the pure helpers — `extract_domain`, `normalize_phone`,
`_parse_and_validate_emails`, `extract_emails_from_html`, and Reacher
response handling in `verify_email_via_reacher` (mocked, no live server
needed). DB-heavy `process_and_deduplicate_leads` and the network
crawler in `harvest_emails_from_websites` are integration-test territory
and are intentionally not covered here.

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
- **`export_history`** — every (`contact_id`, `destination`) push.

Indexes cover the FK columns + all `WHERE`-clause candidates
(`raw_leads.processed_at`, `contacts.lead_status`, etc.).
