# Operations

Durable operator reference for day-to-day use. Current decisions, open work, and non-obvious caveats stay in `CLAUDE.md`.

## Runtime setup

### Python / virtualenv

Preferred local setup:

```bash
pyenv shell 3.12.9
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run tests and CLI entrypoints inside `.venv`.

### Infisical

If `infisical` is installed and this repo is linked via `.infisical.json`, `dev` is already the default environment.

Shortest repo-local form:

```bash
infisical run --path="/$(basename "$PWD")" -- .venv/bin/python run_pipeline.py ...
```

This assumes the Infisical folder name matches the repo directory name (`email-scraper-verifer-cleaner`). If you run from outside the repo root, pass an explicit `--path` or `--project-config-dir`.

### Scraper binary

The project shells out to `gosom/google-maps-scraper`. Put the binary in `app/scraper/`:

| OS | Filename |
|---|---|
| macOS / Linux | `app/scraper/google-maps-scraper` |
| Windows | `app/scraper/google-maps-scraper.exe` |

Selection is OS-aware. `run_scraper.py` picks the right one at runtime and raises `FileNotFoundError` if neither is present.

### Playwright

`grid` requires Playwright setup:

```bash
./scripts/setup_scraper_playwright.sh
```

## CLI entrypoints

### Single location

```bash
.venv/bin/python run_pipeline.py --query "Plumbing" --location "San Jose, CA"
```

### Batch ZIP sweep

```bash
.venv/bin/python run_zip_batch.py --query "HVAC" --zip-file san_jose_zips.csv
```

## Strategy behavior

Three strategies exist on both `run_pipeline.py` and `run_zip_batch.py` via `--strategy {single-centroid, grid, full-harvest}`. `--grid` is shorthand for `--strategy grid`.

| Strategy | Flow |
|---|---|
| `single-centroid` | Depth loop up to `--max-depth`; each iteration scrapes, dedupes, crawls, and can stop early once target new exportable contacts are reached |
| `grid` | One bbox-based scrape using `cell_km`, then one dedupe/crawl pass |
| `full-harvest` | Grid pass 1, slow centroid pass 2 by query variant, optional ZIP top-up pass 3, then one shared dedupe/crawl |

## CLI validation rules

- One vertical per run. A query naming both HVAC and plumbing does not silently run both.
- `--queries` is valid only with `full-harvest`.
- `--min-contacts` and `--max-depth` are single-centroid only.
- `--cell-km` is grid/full-harvest only.
- CSV fallback filenames are descriptive by default and can be overridden with `--csv-path`.

## Verification and export behavior

Verification lives in `app/pipeline/verify_emails.py` and is wired into `run_pipeline.py` via `--verify`.

`run_zip_batch.py` has no `--verify` flag. It accepts `--min-score`, but batch runs do not verify during the run. Verify out of band, then re-export if needed.

Both CLIs end in `export_run_outputs()`, which writes three files from the `--csv-path` base:

| File | Contents | Gated by `--min-score`? | Stamps `export_history`? | Can go to Sheets? |
|---|---|---|---|---|
| `_all` | every contact in the DB | no | no | no |
| `_deduped` | contacts not yet exported to the destination | no | yes | yes |
| `_verified` | best contact per business clearing the score | yes | no | no |

Important consequences:

- `--min-score` gates only `_verified`.
- `_deduped` is the Sheets push and marks contacts exported.
- `_all` appends; rerunning with the same `--csv-path` appends the whole DB again.
- `_verified` is side-effect free and safe to regenerate.

## Proxy and crawler operations

### Proxy files

Common local setup:

```env
SCRAPER_PROXIES_FILE=proxies.txt
CRAWLER_PROXY_FILE=proxies.txt
```

### Block detection and cooldown

- Soft blocks are inferred from low-yield runs.
- Proxy health state lives in `data/proxy_health.json`.
- Full-pool exhaustion can wait out the shortest cooldown before failing.
- Sticky assignment uses a stable hash so the same query variant maps to the same proxy across processes.

### Pacing

`SCRAPER_PACING_SEC` applies only between scraper invocations, never before the first one.

## Crawl-attempt ledger

The crawler tracks `last_crawled_at` and `crawl_attempts` on businesses.

Pending crawl set:

- already has email: done
- `crawl_attempts >= max_attempts`: give up
- inside cooldown window: skip
- otherwise: crawl

## Email filters

Shared junk filtering lives in `app/pipeline/email_filters.py` and applies at ingest, crawl, and export.

`export_sheets._BAD_EMAIL_PREFIXES` stays export-local on purpose. Those addresses are real inboxes that are intentionally excluded from outreach, not junk addresses.

## Scrape timeouts

`SCRAPER_TIMEOUT_SEC` kills the scraper subprocess. On timeout, partial results are salvaged, copied into `logs/`, and downstream dedupe/crawl/export still runs if any leads were recovered.

## Related docs

- `README.md` — setup overview, env skeleton, high-level usage
- `CLAUDE.md` — current decisions, caveats, open work
- `RUNS.md` — run tracker and continuations
- `RUNBOOK_SQL_OVERLAP_ANALYSIS.md` — overlap/lift methodology
- `MAINTENANCE_SQL.md` — backfills and legacy hygiene
