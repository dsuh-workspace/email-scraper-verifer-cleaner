# Run tracker — city × vertical

Source of truth: `database/hvac_leads.db` → `scrape_runs` table (query,
location, category, started_at/completed_at). Check this file before
starting a new city/vertical run; update it after any real production run
completes (not one-off experiments or throwaway DBs).

One vertical per run (see CLAUDE.md "Answered / settled" #4) — don't mix
HVAC and Plumbing in a single row.

| City | Vertical | Date | Strategy | Status | Notes |
|---|---|---|---|---|---|
| San Jose | HVAC | 2026-08-02 | full-harvest, per-variant Pass 2 (lift-table run) | **Done** | Pruned `DEFAULT_HVAC_HARVEST_QUERIES` 8→3 (HVAC, Heating and cooling, HVAC contractor) from this run's yield data. |
| San Jose | Plumbing | 2026-07-30 | full-harvest, grid + **combined** Pass 2 + ZIP top-up | **Stale — recommend re-run** | Predates the `--pass2-per-variant` fix (shipped 2026-08-02). Combined Pass 2 likely suffered the shared-deduper underperformance bug documented in CLAUDE.md 2026-08-02 — same class of issue that made the HVAC lift-table worth doing. TODO #3. |
| Santa Clarita | HVAC | — | — | **Not run** | No trace in main DB. |
| Santa Clarita | Plumbing | — | — | **Not run** | Only artifact is a disposable 2026-07-21 grid-methodology test in `database/hvac_leads.santa_clarita_city.db` (5 shallow runs, generic `query=Plumbing`, pre-fix hardcoded `category="HVAC/Plumbing"`). Built to validate grid coverage, not a lead-gen sweep — doesn't count. |
| Santa Clara, CA + Sunnyvale | Plumbing | 2026-08-02 | ZIP top-up (fast mode) | Done | **Different city from Santa Clarita** (South Bay, not LA County) — don't confuse the two when picking the next city to run. |

## Conventions

- Row = one real production run against the live `database/hvac_leads.db`.
  Experiments, methodology tests, and throwaway/per-city DBs (e.g.
  `*.san_jose_city.db`, `*.santa_clarita_city.db`) don't get a row here —
  note them inline if relevant, like above.
- "Done" means scraped + crawled for emails, not necessarily exported —
  check `export_history` / `--min-score` outcome separately if that
  matters for the task at hand.
- If a run predates a pipeline fix that changes yield (e.g. the Pass 2
  per-variant default), mark it **Stale** with the fix and date, don't
  just delete the row — that's the signal to re-run.
