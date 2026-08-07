# email-scraper-verifer-cleaner — Operator Notes

Current operator guide for the HVAC/Plumbing lead-gen pipeline. Keep this
file focused on present-tense behavior, open work, and decisions — if a
fact belongs in one of the docs below, put it there and link.

Purpose: scrape Google Maps → ingest into SQLite → dedupe → crawl sites for
emails → optionally verify via local Reacher → export to Sheets/CSV.
The pipeline also captures rich map details (review count/rating, address,
status, description, place ID).

| Doc | Holds |
|---|---|
| `README.md` | Setup, env vars, CLI usage, proxies, verification, schema |
| `CHANGELOG.md` | Dated shipped history and closed review items |
| `RUNS.md` | Run tracker (city × vertical) and in-flight run continuations |
| `RUNBOOK_SQL_OVERLAP_ANALYSIS.md` | Cohort/lift/overlap analysis queries |
| `MAINTENANCE_SQL.md` | Hand-run backfills, legacy schema catch-up, hygiene |

## Strategies

Three strategies via `--strategy {single-centroid, grid, full-harvest}` on
both `run_pipeline.py` and `run_zip_batch.py`. Default = `single-centroid`;
`--grid` is shorthand for `--strategy grid`. Each strategy is one function
in `run_pipeline.py` and both CLIs call the same three:

| Function | Strategy | Flow |
|---|---|---|
| `run_location_pipeline()` | single-centroid | Depth loop (step +2) up to `--max-depth`; each iteration scrapes → dedupes → crawls, stopping early once `target_new_exportable` new exportable contacts are reached |
| `run_location_grid()` | grid | One bbox-based scrape (`cell_km`, depth=3), then a single dedupe/crawl pass |
| `run_location_full_harvest()` | full-harvest | Grid Pass 1 → per-variant slow-centroid Pass 2 (depth=10, `fast_mode=False`) → optional fast ZIP top-up Pass 3 (depth=3, `fast_mode=True`) → one shared dedupe/crawl |

All three return `LocationRunMetrics` and share the same tail: `--verify`
runs `verify_contacts_emails()`, then `export_run_outputs(min_score=...)`
— **not** `export_new_leads()`, which it calls internally for one of three
outputs (see "Verification and export tail").
`run_end_to_end_pipeline` is geocode + dispatch + that tail.

### Behavior notes

- `grid` requires Playwright via `./scripts/setup_scraper_playwright.sh`.
- Grid and full-harvest raise when Nominatim returns no bounding box rather
  than silently degrading to centroid mode.
- Pass 2 defaults to **per-variant subprocesses** (`--pass2-per-variant`)
  to avoid the vendored scraper's combined-query undercount. The old
  combined behavior is opt-in via `--pass2-combined`, for diagnostics only.
- Default harvest query sets are intentionally pruned: HVAC defaults to 3
  variants and plumbing defaults to 2, based on recent San Jose reruns.
  The published "39% more than grid alone" figure predates both this
  pruning and the per-variant switch — treat it as historical until
  re-measured (Open work #6).

### CLI validation

- One vertical per run. A query that names both HVAC and plumbing does not
  silently sweep both; full-harvest exits 2 unless explicit `--queries`
  are supplied.
- `--queries` is valid only with `full-harvest`; other strategies exit 2.
- `--min-contacts` / `--max-depth` are single-centroid only. Under
  `grid`/`full-harvest` they warn and are ignored; non-positive values
  exit 2.
- `--cell-km` is the mirror image: grid/full-harvest only, warns and is
  ignored under single-centroid. Non-positive values exit 2 on both CLIs,
  checked before strategy dispatch so `--cell-km 0` errors even where the
  flag would otherwise just be ignored.
- CSV fallback filenames are descriptive by default:
  `data/leads_<location>_<query>_<date>.csv` for single-location runs and
  `data/leads_<query>_<date>.csv` for batch runs. `--csv-path` overrides.

### `run_zip_batch.py` deltas

- `--strategy` / `--grid` / `--cell-km` / `--queries` match
  `run_pipeline.py` spelling and validation. `_resolve_strategy` and
  `_resolve_query_variants` are imported from `run_pipeline`, not
  reimplemented.
- `--target-new-exportable` / `--max-depth` / `--stale-iterations` default
  to `None` and are single-centroid only. Effective defaults: 20 /
  `DEFAULT_MAX_DEPTH` / 2. `--stale-iterations` layers onto the same depth
  loop.
- `--cell-km` validation is now shared with `run_pipeline.py`, and
  `DEFAULT_CELL_KM` is imported from it rather than redefined.
- Geocoding: single-centroid geocodes inside `run_location_pipeline`;
  grid/full-harvest geocode in the batch loop to get each row's bbox.
- Batch full-harvest never passes `zip_csv` because the batch already is
  the ZIP sweep.
- A row that fails (unmappable ZIP, scraper error) is logged and skipped;
  the batch continues, and export still runs once at the end.

### Verification and export tail

Verification (`app/pipeline/verify_emails.py`) is wired into
`run_pipeline.py` and is the supported path. Opt in with `--verify`.
Score map is in `README.md` ("Verification"). Verifier failures warn but
are not fatal.

**`run_zip_batch.py` has no `--verify` flag.** It accepts `--min-score`,
but nothing in a batch run verifies, so any `N > 0` yields an empty
`_verified` file unless a prior run verified those contacts. Verify out of
band (`python -m app.pipeline.verify_emails`) and re-export.

Both CLIs end in `export_run_outputs()`, which writes **three** CSVs off
the `--csv-path` base — this is the part most easily misread:

| File | Contents | Gated by `--min-score`? | Stamps `export_history`? | Can go to Sheets? |
|---|---|---|---|---|
| `_all` | every contact in the DB | no | no | no |
| `_deduped` | contacts with no `export_history` row for the destination | **no** | **yes** | yes |
| `_verified` | best contact per business clearing the score | **yes** | no | no |

Consequences worth holding onto:

- `--min-score` gates **only** `_verified`. The `_deduped` push — the one
  that reaches Sheets and marks contacts exported — is called with a
  hardcoded `min_score=0` (`export_sheets.py`). **Open question, not a
  settled decision** (see Open work #7): before the three-file split
  (13f9b4b, 2026-08-02), `--min-score` gated `export_new_leads()` itself —
  the same function now reused for `_deduped` — so it gated the actual
  Sheets push (393a10c, 2026-07-21). The split hardcoded `min_score=0` at
  that call site, silently dropping the gate on Sheets and moving it to
  the local-only `_verified` file instead; neither commit message states
  this as intentional, and the test added alongside it only covers a
  contact scoring *above* the threshold. The "held-back contacts would
  re-export later" rationale for keeping it this way is plausible but
  unconfirmed — don't cite it as prior intent, and don't "fix" it without
  deciding, with the operator, whether unverified leads should reach
  Sheets at all.
- `_all` is opened in **append** mode and ignores `export_history`, so
  re-running with the same `--csv-path` re-appends the whole DB. The
  dated default filename is what keeps runs from stacking.
  `data/archive/MISLABELED_wholedb_export_2026-08-04_all.csv` is this
  having already bitten us once.
- `_all` and `_verified` are always local files; only `_deduped` attempts
  Sheets. With Sheets configured, `_deduped` may not exist on disk at all.
- `_verified` is side-effect free and safe to regenerate.
- `export_new_leads()` and `export_run_outputs()` both take an optional
  `run_cohort_start` (a `scrape_runs.id` cutoff) that scopes every file —
  including `_all` — to `businesses.first_scrape_run_id >= run_cohort_start`.
  Neither CLI wires it to a flag yet; pass it when calling the functions
  directly (e.g. from an analysis script). Unset, behavior is unchanged:
  `_all` is the whole DB. `scripts/analysis/export_cohort.py` remains the
  tool for historical cohorts that need the `MIN(raw_leads.scrape_run_id)`
  fallback for NULL-provenance businesses — this parameter does not do that
  fallback, it filters straight on `first_scrape_run_id`.

### Email junk filters

One list, `app/pipeline/email_filters.py`, applied at all three points an
address can enter or leave: ingest (`process_leads.py`), crawl
(`extract_emails.py`), export (`export_sheets.py`). Add new junk there.

Before 2026-08-06 each path kept its own list and they had drifted (37 /
18 / 14 entries), so 19 domains the crawler rejected still reached
`contacts` whenever the *scraper's* email field supplied the address
rather than the crawl. `tests/test_email_filters.py` asserts the three
paths agree.

`export_sheets._BAD_EMAIL_PREFIXES` (careers@, jobs@, webmaster@) stays
export-local on purpose — those are real inboxes we decline to pitch, not
junk. Filtering them earlier would lose the business outright when it is
the only address on the site.

## Crawl-attempt ledger

Column semantics, env tuning, and the force-re-crawl SQL live in
`README.md` under "Crawl-attempt notes". Two details it doesn't cover:

- Pending set in `extract_emails.py`: already has email → done;
  `crawl_attempts >= max_attempts` → given up; `last_crawled_at` inside
  the cooldown → skip; else crawl.
- `CRAWL_RETRY_AFTER_HOURS=0` and non-integer values are invalid and fall
  back to the 720-hour default rather than erroring.

## Inline email extraction (`-email`)

`execute_scrape_and_ingest(extract_email=...)`. **None (default) = off for
grid (bbox set), on everywhere else**; True/False forces it.

Upstream's `-email` is not a metadata flag. For every place result whose
website passes `IsWebsiteValidForEmail`, `gmaps/place.go:132` spawns a
separate browser visit to that business's own site — and returns `nil` for
the place, so **nothing is emitted until that visit finishes**. 93.8% of
observed raw leads have a website, so it roughly doubles browser work per
result and gates all output behind it.

Tolerable at one centroid. Across a few hundred grid cells it turned a San
Jose sweep into an 1800s timeout with zero rows written (2026-08-07). Note
the failure mode is *time*, not lost entries: `emailjob.go` returns the
entry even when the website fetch errors, so only a killed process loses
work.

With it off, `raw_leads.email` is empty for that run and emails arrive via
`app/pipeline/extract_emails.py` instead — which does the same job with
concurrency, per-host politeness, the crawl-attempt ledger, and the shared
junk filter, none of which the scraper's inline pass has.

## Scrape timeouts

`SCRAPER_TIMEOUT_SEC` (default 1800) kills the subprocess. On timeout the
run now:

- ingests whatever the scraper had already streamed to `-results`,
- copies that file to `logs/timeout_run<ID>_<UTC>.json`,
- records `scrape_runs.status = 'timeout'`,
- **continues the pipeline** if anything was salvaged — dedupe, crawl and
  export still run over what the sweep paid for. Re-raising here discarded
  342 usable leads after a 30-minute grid sweep (2026-08-07) and forced a
  manual recovery every time.
- re-raises `TimeoutExpired` only when **nothing** was salvaged, so an empty
  run never passes for a thin market.

Partial-ness is not lost by continuing: `run_end_to_end_pipeline` snapshots
`MAX(scrape_runs.id)` at start and checks for `timeout` runs above it at the
end, replacing the "PIPELINE EXECUTED SUCCESSFULLY" banner with an explicit
partial-coverage warning naming each truncated run. Both summary helpers
swallow their own errors — a diagnostic must never fail a pipeline that
otherwise worked.

`timeout` is a distinct status from `failed` so a wall-clock truncation
isn't read as a crash, and like `blocked` it's excluded from
`recent_yields()` — a truncated sweep must never become the baseline other
runs are judged against. Block detection is skipped entirely for these: a
truncated run's yield says nothing about the proxies, and scoring it would
strike working ones for a wall-clock problem.

Two limits worth knowing. `subprocess.run(timeout=)` discards stdout/stderr
when it kills the process, so the preserved file is the only record of how
far the sweep got — and it shows *what* was scraped, not *which cell* it
reached (Open work #4). And single-centroid mode emits a JSON array rather
than JSONL, so a killed one is unterminated and nothing is recoverable from
it; the parser logs and continues rather than raising.

## Block detection, proxy cooldown, pacing

Env vars and the operator-facing rules are in `README.md` ("Block detection
and proxy cooldown", "Sticky proxy assignment", "Pacing between
invocations"). Design points that doc doesn't carry:

- Neither this wrapper nor upstream `gosom/google-maps-scraper` detects
  blocks. A soft-blocked run returns few/zero leads and used to be recorded
  as a normal completion. `app/scraper/block_detect.py` infers it from
  yield instead — there is no HTTP/CAPTCHA signal to read.
- New `scrape_runs.status` value: `blocked`. Nothing gates on status, so
  adding it was safe, but the yield-history baseline counts only
  `completed` runs — otherwise two blocked runs in a row make the second
  look normal and the detector goes quiet exactly when it matters.
- A flag is a signal, not a gate: leads from a flagged run are still
  ingested, and `_assess_run_health()` swallows its own errors so a broken
  history query can never lose them.
- Ledger lives in `data/proxy_health.json` (gitignored), not the DB —
  mutable local state an operator may want to reset by hand. Proxies are
  keyed `user@host:port`; passwords are never written.
- A healthy run **decays one strike** rather than resetting to zero. A
  reset lets a proxy alternate block/success forever without ever retiring.
- Full-pool exhaustion **waits** out the shortest cooldown (up to
  `PROXY_WAIT_MAX_SEC`) and retries before raising `ProxyPoolExhausted`.
  With `--scraper-proxy-limit 3` on a 3-proxy pool, one flagged run parks
  everything; failing hard there would kill every remaining row of a ZIP
  batch. Tests set `PROXY_WAIT_MAX_SEC=0` so they never sleep.
- Sticky assignment uses `hashlib.blake2b`, not `hash()` — the builtin is
  salted per process, so it would pick a different proxy for the same
  query on every invocation. Verified stable across separate processes.
- Pacing (`SCRAPER_PACING_SEC`) is off unless set, applies *between*
  invocations only (depth iterations, Pass 2 variants, Pass 3 ZIPs, batch
  rows) and never before the first one. Invalid values warn and disable.

## Run tracking

**Check `RUNS.md` before starting a new city/vertical run.** Update it
after any real production run completes.

For manual SQL evaluation of incremental yield and market overlap between runs, see `RUNBOOK_SQL_OVERLAP_ANALYSIS.md`.

## Open work

1. **`mock` SPREADSHEET_ID ordering (cosmetic).** `export_sheets.py:47`
   already short-circuits before any credential load, so no auth is
   attempted — but it sits *after* the `CREDENTIALS_FILE` existence check,
   so a mock run without a creds file logs the misleading "Credentials file
   not found" warning. Swap the two checks.
2. **Backfill NULL `first_scrape_run_id`** on legacy `businesses` / `contacts`
   rows from `MIN(raw_leads.scrape_run_id)`, so cohort queries stop needing the
   inference fallback described under "Settled decisions". Not done — queries
   are in `MAINTENANCE_SQL.md`.
3. **Surface `--bbox` on `run_zip_batch.py`.** `run_pipeline.py` has it
   (line ~688, parsed by `_parse_bbox`, overrides the Nominatim box for grid
   and full-harvest Pass 1). The batch runner has no equivalent — it geocodes
   each row and takes whatever bbox Nominatim returns, so there is no way to
   tighten a batch row to its dense core. Cell count scales with bbox area,
   so this is the batch-side version of the geometry half of #5.
4. **Stream scraper progress instead of buffering it.**
   `subprocess.run(..., timeout=)` discards stdout/stderr when it kills the
   process, so a timed-out sweep leaves no record of which cell it reached.
   Partial `-results` are now salvaged and copied to `logs/` (see "Scrape
   timeouts"), which recovers the *leads* but not the *position*. Getting
   that needs `Popen` plus a reader thread — deferred as the more invasive
   half of the same problem.
5. **Grid mode does not reproduce its published numbers.**
   Two runs on 2026-08-06 with shipped defaults (`--cell-km 2.0`, Nominatim
   bbox, proxies on) returned **10 and 4 businesses** for San Jose plumbing,
   in 7.6 and 6.3 min. The reference `Dt` experiment got 362 — but ran
   **unproxied**, over a **hand-picked tight bbox** (~27 x 21 km) at **3 km**
   cells, i.e. 72 cells vs ~420 today. Two independent gaps:

   - **Proxy binding.** JS mode binds one proxy per browser context; the
     pool derives as `ceil(concurrency / pages-per-browser)` and upstream
     `-c` defaults to 1, so the default pool is **one context** and every
     cell shares **one** proxy regardless of `--scraper-proxy-limit` or the
     20k-line proxy file. README warns against *pinning* pool size to 1;
     the defaults already do it. Workaround is `--scraper-concurrency 6
     --scraper-pages-per-browser 1 --scraper-proxy-limit 6`.
   - **Geometry.** Nominatim's metro bbox is much larger than the dense
     core the experiment targeted, so default cell counts are ~6x higher
     with most cells in low-density areas.

   A third multiplier was found on 2026-08-07 and **is now fixed**: `-email`
   was passed unconditionally, and upstream spawns a separate browser visit
   to each business's own website per place result, withholding the place
   entry until it returns (`gmaps/place.go:132`). 93.8% of observed raw
   leads have a website, so grid was doing ~2 browser visits per result
   across ~420 cells. `-email` now defaults off for grid — see "Inline email
   extraction" below. Re-test #5 with that in place before digging further.

   Neither of the two low-yield runs was flagged `blocked` — on a fresh DB
   the low-yield rule has no history to compare against (see #6).
6. **Re-measure full-harvest lift — blocked on #5.** The "39% more than grid
   alone" figure (SJ 2026-07-20: grid=362 → +multi-query=473 → +ZIP=504) was
   measured with the 8-variant Pass 2 set **and** the combined Pass 2 call,
   both since changed, so it no longer describes what the code runs — and
   full-harvest now costs ~Nx Pass 2 wall time on the strength of it. The
   2026-08-06 attempt was invalid: Pass 1 is the denominator and it returned
   10 businesses, against Pass 2's 47 and Pass 3's 41. Procedure and the
   Pass 1 sanity floor are in `RUNBOOK_SQL_OVERLAP_ANALYSIS.md` §11.
7. **Decide whether `_deduped` (the Sheets-bound export) should be gated
   by `--min-score`.** It currently isn't — see "Verification and export
   tail" above. Before the three-file split (13f9b4b, 2026-08-02),
   `--min-score` gated the same function now backing `_deduped`, so score
   *did* gate Sheets (393a10c, 2026-07-21); the split hardcoded
   `min_score=0` there without either commit stating that as intentional,
   and without test coverage of a below-threshold contact. Net effect:
   unverified/low-score leads currently reach Sheets and get marked
   exported same as anything else. Resolve one way or the other, then
   update this doc to say which.

Suite state: **267 passed, deterministic** across repeated runs. The
long-standing proxy-order flakiness is fixed on both sides — scraper-side
selection no longer shuffles (sticky assignment replaced it), and the
crawler-side tests assert set membership instead of a fixed order. The
crawler's own deliberate shuffle in `app/pipeline/extract_emails.py` is
unchanged. If proxy tests start failing again, check for leaked env
(`.env` injects `SCRAPER_PROXIES_FILE=proxies.txt`) before suspecting logic.

## Analysis tooling

Cohort/lift analysis lives in `scripts/analysis/` and runs inside `.venv`
(stdlib + SQLAlchemy only — **pandas is not a dependency**, so the retired
root-level `calculate_overlap.py` / `get_runtimes.py` could not run there):

| Script | Purpose |
|---|---|
| `market_overlap.py` | Business/contact overlap and lift for a run cohort. Reuses the pipeline's own dedupe keys; handles NULL provenance and cross-vertical contamination. |
| `export_cohort.py` | Cohort-scoped CSV export. Side-effect free — does not touch `export_history`. |
| `run_wallclock.py` | Active wall-clock time for a cohort, merging overlapping run intervals. |

`market_overlap.py` replaces `calculate_overlap.py`, which matched raw leads to
businesses on `place_id` — a key the pipeline never uses for dedupe.

## Market-overlap setup rules

San Jose ↔ Sunnyvale/Santa Clara overlap came in at **10.8% (plumbing)** and
**12.6% (HVAC)** — the adjacent market is ~87–89% net-new. Numbers, caveats,
and the continuation runbook are in `RUNS.md`; methodology is in
`RUNBOOK_SQL_OVERLAP_ANALYSIS.md`.

Two rules before starting any market test, both learned the hard way:

- **Seed the candidate DB from a single-vertical baseline.** A shared baseline
  puts same-market/different-vertical runs below the cohort cutoff, where they
  get miscounted as overlap (this inflated HVAC 12.6% → 19.6%).
- **One pipeline process per DB.** Three concurrent HVAC processes interleaved
  run IDs and made the wall-clock figure incomparable to a sequential run.

## Intentional deferrals

- **Export pushes empty-email rows to Sheets** — deliberate project
  decision. Blank-email contacts may still export.
- **Commits inside per-business loop** — batching every 25 is
  acceptable for now.
- **No `robots.txt` / no per-domain politeness** — per-host locking is
  in place; `robots.txt` is still ignored.

## Environment / operational

### Python / venv conventions

Setup is in `README.md` ("Python version / virtualenv"). Run every Python
command — tests and CLI entrypoints included — inside `.venv`.

### Local Reacher instance

URL, start script, and score map are in `README.md` ("Verification").
Operator details it doesn't cover:

- `start_local_verifier.sh` no-ops if the endpoint is already reachable
  and restarts an existing stopped `reacher-backend` container when it
  can, so re-running it is safe.
- `./scripts/stop_local_verifier.sh` stops and removes the container.
- No auth on the endpoint itself.
- Apple Silicon runs the published Docker image under emulation.

### Scraper auto-update

- `scripts/update_scraper.sh` pulls `../google-maps-scraper`, rebuilds,
  smoke-tests, and only swaps in the new binary if output still matches
  what `run_scraper.py` expects.
- Weekly launchd job: `com.apl.update-scraper` (Sunday 03:17).
- Tracked plist: `scripts/com.apl.update-scraper.plist`.
- Check status with `launchctl print gui/$(id -u)/com.apl.update-scraper`.
- Launchd logs to `logs/update_scraper.launchd.log`; script logs to
  `logs/update_scraper.log`.

### DB migration notes

`init_db()` runs `_apply_additive_columns()` after `create_all()` to add
missing additive columns, currently:

- `businesses.last_crawled_at`
- `businesses.crawl_attempts`
- `businesses.first_scrape_run_id`
- `contacts.first_scrape_run_id`
- `export_history.exported_at`

This is idempotent and additive-only. Backfills, non-additive changes, and
legacy-DB catch-up live in `MAINTENANCE_SQL.md`.

Provenance stamping, as currently implemented:

- `process_and_deduplicate_leads()` copies `RawLead.scrape_run_id` onto new
  `Business` / `Contact` rows as `first_scrape_run_id`.
- `harvest_emails_from_websites()` stamps crawl-discovered contacts using
  `MAX(scrape_runs.id)` at harvest start. The crawl is not itself a scrape
  run, so its contacts are attributed to the cohort whose pipeline
  invocation produced them.

Rows written before 2026-08-04 predate the crawl-path stamping and carry
NULL provenance — see "Settled decisions" below for how analysis scripts
work around it, and Open work #2 for the backfill that would retire the
workaround.

## Settled decisions

- Sheets export stays. `SPREADSHEET_ID=mock` should fall back to CSV; the
  remaining bug is the auth short-circuit.
- Category is not hardcoded. Scraper-reported categories win; query is the
  fallback.
- `min_contacts` means new exportable contacts produced by this run, not
  cumulative DB contacts.
- One vertical per run: HVAC and plumbing are not combined implicitly.
- Lift/overlap analysis relies on `businesses.first_scrape_run_id` /
  `contacts.first_scrape_run_id`, not raw-lead first-seen inference.
  **Two caveats apply to data written before 2026-08-04:** legacy rows
  have NULL provenance and were never backfilled (Open work #2), and
  crawl-created contacts were never stamped at all. For historical
  cohorts, scope contacts by their *business's* provenance and fall back
  to `MIN(raw_leads.scrape_run_id)` for NULL businesses.
  `scripts/analysis/market_overlap.py` does both.

## Notes

- `max_depth=20` is mostly legacy compatibility for single-centroid.
- Prefer `new_exportable_contacts` over `total_contacts` when evaluating
  batch runs.
- `new exportable` does not mean verified or even non-blank-email-only.
- Broad ZIP sweeps can still surface junk emails; spot-check exports
  before outreach or verification.

See `CHANGELOG.md` for shipped history and closed review items.
