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
Email Harvest (crawl contact/about/team/legal pages; regex extract)
        ↓
Email Verify (self-hosted Reacher)   [opt-in: --verify]
        ↓
export_run_outputs() → three CSVs: _all / _deduped / _verified
        (_deduped is the Sheets push + export-history dedupe)
```

The verify step is wired into `run_pipeline.py` and runs after the email
crawl, before export — opt in with `--verify`. `--min-score N` filters the
`_verified` CSV only; it does **not** gate the Sheets/export-history push.
See [Verification](#verification) and [Export outputs](#export-outputs).

---

## Setup

### 1. Prerequisites

- Python 3.10+
- macOS or Linux (Windows works too, use the `.exe` binary variant)
- Google Maps scraper binary (see below)

### Python version / virtualenv

Preferred local setup:

```bash
pyenv shell 3.12.9
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

This repo now includes `.python-version` with `3.12.9`, so `pyenv`
should auto-select the right interpreter when you `cd` into the repo.
Run tests and CLI entrypoints inside `.venv`.

### 2. Install

```bash
python -m venv .venv
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

# Reacher email verifier (self-hosted locally; see below)
REACHER_API_URL=http://127.0.0.1:8080/v0/check_email
REACHER_TIMEOUT_SEC=30

# Optional proxy config
SCRAPER_PROXIES=http://user:pass@proxy1.example.com:8080,socks5://proxy2.example.com:1080
SCRAPER_PROXIES_FILE=proxies.txt
# Optional scraper tuning defaults
# SCRAPER_TIMEOUT_SEC=1800         # hard ceiling per scraper invocation (30 min)
# SCRAPER_CONCURRENCY=3
# SCRAPER_BROWSER_POOL_SIZE=       # leave unset; see "Scraper runtime knobs"
# SCRAPER_PAGES_PER_BROWSER=1
# SCRAPER_PROXY_LIMIT=3
# SCRAPER_DISABLE_PAGE_REUSE=1   # not read by wrapper; use CLI flag today

# Optional pacing between scraper invocations (MIN:MAX seconds; off if unset)
# SCRAPER_PACING_SEC=10:20

# Optional block detection / proxy cooldown tuning (defaults shown)
# BLOCK_DETECT_ENABLED=1
# BLOCK_DETECT_ZERO_YIELD=1        # treat a 0-lead proxied run as a block
# BLOCK_DETECT_MIN_HISTORY=3       # runs needed before the median rule applies
# BLOCK_DETECT_LOW_YIELD_RATIO=0.25
# BLOCK_DETECT_HISTORY_LIMIT=10    # how many prior runs feed the median
# PROXY_HEALTH_FILE=data/proxy_health.json
# PROXY_COOLDOWN_SEC=600           # first strike
# PROXY_RETIRE_AFTER_STRIKES=2
# PROXY_RETIRE_SEC=86400           # effectively "retired for the run"
# PROXY_WAIT_MAX_SEC=900           # max wait when the whole pool is cooling; 0 = fail fast
CRAWLER_PROXY=http://user:pass@proxy3.example.com:8080
CRAWLER_PROXY_FILE=proxies.txt
# Or split crawler proxies by scheme
# CRAWLER_HTTP_PROXY=http://proxy-http.example.com:8080
# CRAWLER_HTTPS_PROXY=https://proxy-https.example.com:8443

# Optional crawl-attempt ledger tuning (defaults shown)
# CRAWL_RETRY_AFTER_HOURS=720   # cooldown before re-crawling a site that
#                               # yielded no email (30 days)
# CRAWL_MAX_ATTEMPTS=3          # give up after N consecutive no-email
#                               # attempts; 0 = no cap

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
# Disable both scraper + crawler proxies for this run
python run_pipeline.py --query "Plumbing" --location "San Francisco, CA" --no-proxy
# Conservative: 3 proxies, one browser each, one tab per browser.
# Leave --scraper-browser-pool-size unset so upstream derives the pool as
# ceil(concurrency / pages-per-browser) = 3 browsers -> 3 proxies in play.
python run_pipeline.py \
  --query "Plumbing" \
  --location "San Jose, CA" \
  --scraper-proxy-limit 3 \
  --scraper-pages-per-browser 1 \
  --scraper-concurrency 3

# Same 3 proxies, two tabs each, once proxies have proven themselves.
python run_pipeline.py \
  --query "Plumbing" \
  --location "San Jose, CA" \
  --scraper-proxy-limit 3 \
  --scraper-pages-per-browser 2 \
  --scraper-concurrency 6
```

Do **not** pin `--scraper-browser-pool-size 1` to "be gentle". In JS mode a
proxy is bound per browser context, so a one-browser pool puts the whole pass
behind a single proxy and makes `--scraper-proxy-limit` almost meaningless.
Lower `--scraper-concurrency` instead.

`run_zip_batch.py` exposes the same scraper tuning knobs, including
`--scraper-disable-page-reuse`, and forwards them into the selected
strategy for each ZIP (see [Batch strategies](#batch-strategies)).

Defaults:
- `--min-contacts 500` (applied when the flag is omitted; single-centroid only)
- `--max-depth 20` (applied when the flag is omitted; single-centroid only)
- scraper concurrency/browser pool use upstream defaults unless overridden
  (upstream concurrency is effectively `1`, i.e. serial)
- scraper pages per browser defaults to current wrapper value `2`
- scraper forwards `3` validated proxies by default unless overridden

Scraper runtime knobs exposed by this wrapper:
- `--scraper-concurrency` → upstream `-c`. Upstream's own default is
  effectively `1`, so leaving this unset means serial scraping.
- `--scraper-browser-pool-size` → upstream `-browser-pool-size`. Leave unset
  (see the warning above); unset means upstream derives
  `ceil(concurrency / pages-per-browser)`.
- `--scraper-pages-per-browser` → upstream `-pages-per-browser`. Upstream
  ignores a value of `1` (its own default), but this wrapper defaults to `2`,
  so passing `1` does change the run.
- `--scraper-proxy-limit` → wrapper-side cap; forwards `N` of the validated
  proxies, chosen as a rotating window keyed by the query variant.
- `--scraper-disable-page-reuse` → upstream `-disable-page-reuse`

**Which knobs apply depends on the strategy**, because only JS mode drives a
browser:

| Strategy | Mode | Browser knobs apply? | Proxy rotation |
|---|---|---|---|
| `single-centroid` | fast (`-fast-mode`) | No — no browser at all | per request |
| `grid` | JS | Yes | per browser context |
| `full-harvest` Pass 1 | JS | Yes | per browser context |
| `full-harvest` Pass 2 | JS (`fast_mode=False`) | Yes | per browser context |
| `full-harvest` Pass 3 | fast | No | per request |

So tuning the browser pool only affects `grid` and full-harvest Passes 1–2.
Under `single-centroid` those flags are accepted and forwarded but inert.

Not exposed here today (they need upstream changes — the scraper is a Go
subprocess that takes no jitter arguments):
- per-action delay / jitter
- scroll timing or human-ish cadence

### Pacing between invocations

Per-action jitter is not reachable, but the gap between scraper invocations
is. Set `SCRAPER_PACING_SEC=MIN:MAX` (seconds) to sleep a jittered interval
before each invocation after the first:

- single-centroid: before each depth-loop iteration
- full-harvest: before each Pass 2 variant and each Pass 3 ZIP
- `run_zip_batch.py`: before each row after the first

Off when unset, so run wall-clock stays comparable to the figures in
`RUNS.md` until you opt in. A bare number (`SCRAPER_PACING_SEC=15`) is a fixed
delay. Invalid values warn and disable pacing rather than failing the run.

### Block detection and proxy cooldown

Neither this wrapper nor upstream inspects pages for Google's `/sorry/`
interstitial or a captcha, so a soft-blocked run used to look exactly like a
thin market: few or zero leads, recorded as `completed`. Yield is the only
signal that survives the subprocess boundary, so runs are now judged on it:

- A proxied run that ingests **zero** leads is flagged `zero-yield`.
- A run yielding less than `BLOCK_DETECT_LOW_YIELD_RATIO` (default 0.25) of
  the median for the same query + location is flagged `low-yield`, once
  `BLOCK_DETECT_MIN_HISTORY` (default 3) prior `completed` runs exist.

A flagged run is stored as `scrape_runs.status = 'blocked'` instead of
`'completed'`, and every proxy it used takes a strike in
`data/proxy_health.json`: first strike parks the proxy for
`PROXY_COOLDOWN_SEC`, `PROXY_RETIRE_AFTER_STRIKES` strikes park it for
`PROXY_RETIRE_SEC`. A healthy run decays one strike. Blocked runs are excluded
from future medians so a bad streak cannot drag the baseline down to meet it.

Leads from a flagged run are still ingested — the flag is a signal for the
operator, not a gate. Notes:

- Detection never fails a run: if the history query errors it logs and skips.
- If every proxy is cooling, the run **waits** for the shortest cooldown to
  expire (up to `PROXY_WAIT_MAX_SEC`, default 900s) and retries. This matters
  with a small pool: `--scraper-proxy-limit 3` against a 3-proxy pool means one
  flagged run parks everything, and failing hard there would take the rest of a
  ZIP batch down with it.
- Only if the wait would exceed the cap — a retirement-length park, or
  `PROXY_WAIT_MAX_SEC=0` — does the run raise `ProxyPoolExhausted`, rather than
  scraping from your own IP. Add proxies or delete `data/proxy_health.json` if
  the strikes were false positives. `run_zip_batch.py` logs the failed row and
  continues.
- The median is noisy by design: full-harvest runs the same query and location
  at different depths and modes, which legitimately yield very different
  counts. Hence the low default ratio.
- Turn the whole thing off with `BLOCK_DETECT_ENABLED=0`.

Inspect the ledger and recent flags with:

```bash
cat data/proxy_health.json
sqlite3 database/hvac_leads.db \
  "SELECT id, query, location, status FROM scrape_runs
   WHERE status='blocked' ORDER BY id DESC LIMIT 20;"
```

### Sticky proxy assignment

`--scraper-proxy-limit N` picks `N` proxies as a rotating window over the
validated pool, offset by a stable hash of the query variant. The same variant
therefore draws the same proxies on every invocation, while different variants
spread across the pool.

This replaced a random shuffle, which redrew a random subset per invocation —
so no variant could hold a proxy across its run, and a proxy that got a run
blocked was both unattributable and immediately eligible again. Proxies in
cooldown are removed before the window is computed.

### Override campaign settings

```bash
python run_pipeline.py \
  --query "HVAC" \
  --location "Plano, TX" \
  --min-contacts 50 \
  --max-depth 9
```

`--min-contacts` counts **new exportable contacts produced by this run**
(contacts with an email that have no `export_history` row for the
destination yet), not cumulative DB contacts — so re-running against a
populated DB still scrapes. Both flags are **single-centroid only**;
grid and full-harvest don't loop on depth and warn if you pass either.
Both must be `> 0` — a non-positive value exits with status 2.

### Grid-mode scraping (recommended for coverage)

```bash
python run_pipeline.py \
  --query "Plumbing" \
  --location "San Jose, CA" \
  --grid \
  --cell-km 2.0
```

Uses the scraper's native `-grid-bbox` mode (JS mode via Playwright).
Iterates cells over the location's Nominatim-derived bounding box in one
scraper invocation.

> **The published grid numbers do not describe this command.** The
> "4-25× more unique businesses than single-centroid" and "362 businesses
> for San Jose plumbing" figures come from the 2026-07-20 `Dt` experiment,
> which ran **unproxied**, over a **hand-picked tight bbox**
> (`37.20,-121.99,37.44,-121.75`, ~27 × 21 km) at **3 km** cells — 72 cells
> in 10.1 min. The defaults above give a materially different run: San
> Jose's Nominatim bbox is 40.7 × 38.4 km, which at `--cell-km 2.0` is
> **~420 cells**, and proxies are on.
>
> Measured 2026-08-06 with those defaults: **10 and 4 businesses** across
> two runs (6.3 and 7.6 min). See "Grid mode and proxy binding" below
> before running a grid pass you intend to trust.

#### Grid mode and proxy binding

In JS mode the scraper binds **one proxy per browser context**, and the
default browser pool is **one context** — upstream `-c` defaults to 1 and
the pool derives as `ceil(concurrency / pages-per-browser)`. So a default
grid run puts every cell behind a **single** proxy, no matter how large
your pool is or what `--scraper-proxy-limit` says. Several hundred cells
through one IP in a few minutes is the shape of a run that gets soft-blocked
partway through and returns a fraction of its yield.

The existing warning below ("Do not pin `--scraper-browser-pool-size 1`")
understates this: you do not have to pin it — **the defaults already give
you a pool of one.** To spread a grid pass across proxies, raise
concurrency so the derived pool is larger:

```bash
python run_pipeline.py \
  --query "Plumbing" --location "San Jose, CA" --grid --cell-km 2.0 \
  --scraper-concurrency 6 \
  --scraper-pages-per-browser 1 \
  --scraper-proxy-limit 6        # -> 6 browser contexts, 6 proxies
```

Tightening the bbox with `--bbox` to the dense core also cuts cell count
directly, which is what the reference experiment did.

One-time setup: run `./scripts/setup_scraper_playwright.sh` to install the
Playwright driver + Chromium + FFmpeg (~265 MB). Also handles the
mxschmitt/playwright-go v0.6100.0 version-mismatch workaround.

Optional: `--bbox min_lat,min_lon,max_lat,max_lon` overrides the
Nominatim-derived bbox when you want a specific region.

Grid mode ignores both `--max-depth` (single scrape at depth 3 per cell)
and `--min-contacts`; passing either logs a warning.

### Full-harvest strategy (max coverage)

```bash
python run_pipeline.py \
  --query "Plumbing" \
  --location "San Jose, CA" \
  --strategy full-harvest \
  --cell-km 2.0 \
  --zip-csv san_jose_zips.csv
```

Three passes over the market, each ingesting into `raw_leads`:

1. **Grid** — cells over Nominatim bbox (or `--bbox`), JS mode, depth 3.
2. **Multi-query slow at centroid** — semantic variants of the query at
   depth 10, each run as its **own** scraper invocation (one `scrape_runs`
   row per variant). Defaults are deliberately small — plumbing:
   `Plumbing, Plumber`; HVAC: `HVAC, Heating and cooling, HVAC contractor`
   — because a per-variant lift table showed the other variants of the
   original 8-variant set contributing ~0 net-new businesses over Pass 1.
   Override with `--queries "a,b,c"`.
3. **Fast ZIP top-up** *(optional)* — one fast-mode scrape per ZIP in
   `--zip-csv` (`zip,city,state` columns). ~2 s each.

Pass 2 runs one subprocess per variant rather than one combined
multi-query call, because the vendored Go scraper shares a deduper/exiter
across every line of a non-grid `-input` file and silently drops most
variants' results (SJ HVAC 2026-08-01/02: 4 raw leads combined vs 81 run
separately). `--pass2-combined` opts back in, for diagnostics only. The
cost is roughly Nx Pass 2 wall time.

> **Coverage claim is stale, and cannot currently be re-measured.**
> Full-harvest was measured at 39% more unique businesses than grid alone
> (SJ 2026-07-20: grid=362 → +multi-query=473 → +ZIP=504, see
> `plans/scrape-strategy-experiments-2026-07-20.md`). That run used the
> 8-variant set *and* the combined call, both since changed.
>
> A 2026-08-06 re-measurement attempt failed because Pass 1 — the grid
> baseline, i.e. the denominator — returned 10 businesses under default
> settings (see the grid warning above). Passes 2 and 3 worked normally
> (47 and 41 net-new). Until a grid pass yields something comparable to
> the reference, there is no valid baseline to measure lift against.
> `RUNBOOK_SQL_OVERLAP_ANALYSIS.md` §11 has the procedure and the Pass 1
> floor to check first.

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
- a row that fails (unmappable location, scraper error) is logged and
  skipped; the batch keeps going
- exports once at batch end, via the same `export_run_outputs()` as
  `run_pipeline.py` (see [Export outputs](#export-outputs))
- **no `--verify` flag.** `run_zip_batch.py` accepts `--min-score`, but
  nothing in a batch run verifies, so any `N > 0` produces an empty
  `_verified` CSV unless a previous run verified those contacts. To verify
  a batch, run `python -m app.pipeline.verify_emails` afterwards and then
  re-run the export.

`run_pipeline.py --strategy single-centroid` shares this same depth loop
(`run_location_pipeline`), so its `--min-contacts` means the same thing as
`--target-new-exportable` here.

The scraper depth starts at 1 and grows by 2 each iteration up to
`--max-depth`. The location is geocoded **once** at pipeline start and passed
into every subsequent scrape iteration — Nominatim ToS friendly.

#### Batch strategies

`run_zip_batch.py` takes the same `--strategy` flag as `run_pipeline.py`
and applies it to every row, sharing the same three strategy
implementations:

```bash
# grid over each ZIP's own bounding box
python run_zip_batch.py \
  --query "Plumbing" \
  --zip-file san_jose_zips.csv \
  --grid --cell-km 2.0

# full-harvest per row: grid pass + multi-query centroid sweep
python run_zip_batch.py \
  --query "Plumbing" \
  --zip-file san_jose_zips.csv \
  --strategy full-harvest \
  --queries "Plumber,Drain cleaning,Water heater repair"
```

- Default stays `single-centroid`, so existing invocations are unchanged.
- `--target-new-exportable`, `--max-depth`, and `--stale-iterations` are
  **single-centroid only** — grid and full-harvest run a fixed set of
  passes, so passing any of them logs a warning and is ignored.
  `--cell-km` is likewise ignored (with a warning) under single-centroid.
- Batch full-harvest skips Pass 3 (the fast ZIP top-up) — the batch is
  already a ZIP sweep, so there is no `--zip-csv` to pass. Each row still
  costs a grid pass **plus** a multi-query centroid sweep; the run warns
  about the total up front.
- `--queries` overrides the Pass 2 variant set for every row. Omit it and
  the variants are derived from `--query`; as with `run_pipeline.py`, a
  query that names neither trade or both exits with status 2.

Proxy notes:
- `--no-proxy` disables both scraper and crawler proxies for one run.
- `--no-scraper-proxy` disables only scraper proxies for one run.
- `--no-crawler-proxy` disables only crawler proxies for one run.
- `SCRAPER_PROXIES` passes comma-separated proxies straight to gosom `-proxies`.
- `SCRAPER_PROXIES_FILE` loads one proxy per line and appends them to `SCRAPER_PROXIES`.
- `CRAWLER_PROXY` applies one HTTP/HTTPS proxy to website crawling.
- `CRAWLER_PROXY_FILE` loads one proxy per line; crawler uses first valid entry.
- `CRAWLER_HTTP_PROXY` and `CRAWLER_HTTPS_PROXY` override `CRAWLER_PROXY` / `CRAWLER_PROXY_FILE` per scheme.
- Proxy file lines may be full URLs (`http://user:pass@host:port`) or compact Webshare-style lines (`host:port:user:password`).
- Crawler proxy support accepts `http`, `https`, `socks5`, and `socks5h` when provided as full proxy URLs. Compact proxy-file lines normalize to `http://...` URLs.

Crawl-attempt notes:
- Every crawl stamps `businesses.last_crawled_at` and bumps
  `businesses.crawl_attempts` — whether or not an email was found, and
  including attempts that error out.
- A site that yields no email is skipped for `CRAWL_RETRY_AFTER_HOURS`
  (default 720 = 30 days), then retried. After `CRAWL_MAX_ATTEMPTS`
  (default 3) consecutive no-email attempts it is skipped permanently.
  Set `CRAWL_MAX_ATTEMPTS=0` to keep retrying forever on the cooldown.
- Finding an email resets `crawl_attempts` to 0, so a site that starts
  publishing an address isn't pinned at the give-up threshold.
- This is what stops the depth loop from re-fetching the same email-less
  domains on every iteration. To force a full re-crawl of a DB, clear the
  ledger: `UPDATE businesses SET last_crawled_at = NULL, crawl_attempts = 0;`

---

## Verification

Email verification is done against a self-hosted [Reacher
`check-if-email-exists`](https://github.com/reacherhq/check-if-email-exists)
backend. The supported instance is **local**:

- URL: `http://127.0.0.1:8080/v0/check_email`
- Start it with `./scripts/start_local_verifier.sh`. This uses Docker (via `reacherhq/backend:latest`) or compiles the backend from source via Cargo from the `../email-verifier` directory.
- A remote Kamatera deployment was the original host (Hetzner blocks
  outbound SMTP port 25 for new accounts; Kamatera does not). It is legacy
  — its deploy scripts live in the sibling repo
  `autopilotlocal/email-verifier`. Point `REACHER_API_URL` at it only if
  you have re-provisioned it.

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

A BillionVerify-based implementation preceded this one. It has been
removed; see `CHANGELOG.md` for the switch.

Verification is wired into the main pipeline — `run_pipeline.py --verify`
calls `verify_contacts_emails()` between `harvest_emails_from_websites()`
and `export_run_outputs()`, for all three strategies. `--min-score N`
filters the `_verified` CSV:

```bash
python run_pipeline.py --query "Plumbing" --location "San Jose, CA" \
  --verify --min-score 50
```

Verifier failures are logged as warnings and the pipeline continues. An
unreachable Reacher instance scores every contact `unknown` (25), so
`--min-score 50` against a dead verifier yields an empty `_verified` file
— check the log for verifier warnings before concluding the harvest was
empty.

`run_zip_batch.py` has no `--verify` flag; see
[Batch zip-file mode](#batch-zip-file-mode).

---

## Export outputs

Both entrypoints finish by calling `export_run_outputs()`, which writes
**three** CSVs derived from the `--csv-path` base (default
`data/leads_<location>_<query>_<date>.csv`):

| File | Contents | `--min-score` applies? | Writes `export_history`? | Can reach Sheets? |
| ---- | -------- | ---------------------- | ------------------------ | ----------------- |
| `<base>_all.csv` | every contact in the DB, joined to its business | no | no | no |
| `<base>_deduped.csv` | contacts with an email and no `export_history` row for the destination | **no** | **yes** | yes |
| `<base>_verified.csv` | best contact per business clearing the score | **yes** | no | no |

Read that table before trusting a run:

- **`--min-score` gates only `_verified`.** The `_deduped` push — the one
  that goes to Sheets and marks contacts as exported — always runs
  unfiltered. This is intentional: if score decided what counted as
  "already sent", a contact withheld today would re-export later once it
  was verified. `_verified` is the file to hand to outreach.
- **`_all` is opened in append mode** and ignores `export_history`, so
  reusing a `--csv-path` across runs re-appends the entire DB. The dated
  default filename is what keeps that from happening.
- **`_all` and `_verified` are always written locally** and never pushed to
  Sheets. Only `_deduped` attempts Sheets, with CSV as its fallback — so
  with Sheets configured, `<base>_deduped.csv` may not exist on disk.
- `_verified` is side-effect free, so it is safe to regenerate.

Note that `export_new_leads()` (the `_deduped` path) has no run-cohort
filter: on a DB carrying a baseline it emits every unexported contact, not
just this run's. Use `scripts/analysis/export_cohort.py` for a
cohort-scoped, side-effect-free CSV.

### Junk-email filtering

`app/pipeline/email_filters.py` holds one blocklist, applied at all three
points an address can enter or leave the system: ingest
(`process_leads.py`), website crawl (`extract_emails.py`), and export
(`export_sheets.py`). Add new junk domains there and every path picks it
up. The export path layers its own prefix rules (`careers@`, `jobs@`,
`webmaster@`) on top — those are real inboxes that simply aren't sales
leads, so they are dropped only at export.

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
`extract_emails_from_html`, junk-filter parity across the ingest/crawl/
export paths, Reacher response handling in `verify_email_via_reacher`
(mocked, no live server needed), scraper proxy parsing, and
`run_pipeline` / `run_zip_batch` control-flow behavior such as batch init
and continue-on-error handling. DB-heavy `process_and_deduplicate_leads`
and live network crawling in `harvest_emails_from_websites` are still
integration-test territory and are intentionally not covered here.

`tests/conftest.py` sets `DATABASE_URL=sqlite:///:memory:` before any
`app.db.database` import so tests never touch a real DB.

---

## Project structure

```
├── app/
│   ├── logging_config.py       # Central logging setup
│   ├── proxy_utils.py          # Proxy line parsing + URL validation
│   ├── db/
│   │   ├── create_tables.py    # SQLAlchemy models + init_db()
│   │   └── database.py         # Engine + DATABASE_URL guard
│   ├── pipeline/
│   │   ├── email_filters.py    # Shared junk-email blocklist (all 3 paths)
│   │   ├── export_sheets.py    # Sheets + the three-CSV export
│   │   ├── extract_emails.py   # Concurrent multi-path website crawler
│   │   ├── process_leads.py    # Clean + dedupe (batch preloaded)
│   │   └── verify_emails.py    # Reacher API client
│   └── scraper/
│       ├── google-maps-scraper[.exe]  # Compiled Go binary (gitignored)
│       ├── block_detect.py     # Yield-based soft-block detection
│       ├── pacing.py           # Jittered sleep between invocations
│       ├── proxy_health.py     # Strike/cooldown ledger
│       └── run_scraper.py      # Subprocess wrapper + geocoder
├── scripts/
│   ├── analysis/               # Cohort overlap, lift, wall-clock
│   ├── setup_scraper_playwright.sh
│   ├── start_local_verifier.sh / stop_local_verifier.sh
│   └── update_scraper.sh       # Weekly launchd job
├── tests/                      # Pytest suite
├── data/                       # CSV exports + proxy_health.json (gitignored)
├── database/hvac_leads.db      # SQLite (gitignored)
├── .env                        # Local config (gitignored)
├── requirements.txt
├── run_pipeline.py             # Single-location entrypoint
├── run_zip_batch.py            # Batch entrypoint (CSV of zips/locations)
├── CLAUDE.md                   # Operator notes + backlog
├── CHANGELOG.md                # Shipped history
├── RUNS.md                     # Run tracker (city x vertical)
├── RUNBOOK_SQL_OVERLAP_ANALYSIS.md
├── MAINTENANCE_SQL.md          # Backfills + legacy schema catch-up
└── README.md
```

---

## Database schema

- **`scrape_runs`** — one row per scraper invocation.
- **`raw_leads`** — scraper output, tagged `processed_at` after promotion.
- **`businesses`** — canonical deduped businesses, `domain` UNIQUE. Also
  carries the crawl-attempt ledger (`last_crawled_at`, `crawl_attempts`)
  the email harvester uses to avoid re-crawling sites that yielded nothing.
- **`contacts`** — one row per person/inbox; `(business_id, email)` UNIQUE.
- **`email_verifications`** — Reacher results, one per contact.
- **`export_history`** — every (`contact_id`, `destination`) push, with `exported_at` timestamp.

Indexes cover the FK columns + all `WHERE`-clause candidates
(`raw_leads.processed_at`, `contacts.lead_status`, etc.).

If you have an older DB, add/backfill `export_history.exported_at` before relying on that field in reporting or audits. Legacy DBs from before the case-insensitive URL normalization fix may also contain bad `businesses.domain` values like `http:` that need manual cleanup.
