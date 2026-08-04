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
| San Jose | Plumbing | 2026-08-02 | full-harvest, per-variant Pass 2 | **Done** | Completed on fresh DB `database/hvac_leads.san_jose_plumbing_rerun_2026-08-02c.db`. Outputs: `data/leads_sanjose_plumbing_rerun_2026-08-02c_verified.csv` (`_all` / `_deduped` moved to `data/archive/`). Final run: 68 contacts total, 14 new exportable, 0 website-harvested email contacts. Pass 2 was heavily concentrated in `Plumbing` and `Plumber`; the other six variants produced empty/missing output files on this run, so the plumbing lift-table/pruning follow-up remains open. |
| Santa Clarita | HVAC | 2026-08-02 | full-harvest, per-variant Pass 2 | **Done** | Completed on fresh per-city DB `database/hvac_leads.santa_clarita_hvac_2026-08-02_fresh.db` (not the shared main DB). Outputs: `data/leads_santa_clarita_hvac_2026-08-02_final.csv` (28 rows, outreach-ready) and `_verified.csv` (35 rows, pre-cleanup); `_all` / `_deduped` moved to `data/archive/`. Quality caveat: `_verified` includes off-domain crawl emails (`member_services@rioradio.org`, `astigma@astigmatic.com`, `flags@2x.png`) — stripped in `_final`. |
| Santa Clarita | Plumbing | 2026-08-02 | full-harvest, per-variant Pass 2 | **Done** | Completed on fresh per-city DB `database/hvac_leads.santa_clarita_plumbing_2026-08-02_fresh.db` (not the shared main DB). Outputs: `data/leads_santa_clarita_plumbing_2026-08-02_final.csv` (71 rows, outreach-ready) and `_verified.csv` (245 rows, pre-cleanup); `_all` / `_deduped` moved to `data/archive/`. Large contamination in `_verified` from off-domain/support-site emails (e.g. `imtresidential.com`, `newapthome.com`, `santaclarita.gov`, `latofonts.com`) — filtered out in `_final`, which is the file to use for outreach. |
| Santa Clara, CA + Sunnyvale | Plumbing | 2026-08-02 | ZIP top-up (fast mode) | Done | **Different city from Santa Clarita** (South Bay, not LA County) — don't confuse the two when picking the next city to run. |

## Conventions

- Row = one real production run. Prefer the live `database/hvac_leads.db`,
  but if a run is intentionally isolated to a fresh per-city DB for yield or
  concurrency reasons, record it here and name the DB explicitly in Notes.
  Experiments and methodology tests still don't count.
- "Done" means scraped + crawled for emails, not necessarily exported —
  check `export_history` / `--min-score` outcome separately if that
  matters for the task at hand.
- If a run predates a pipeline fix that changes yield (e.g. the Pass 2
  per-variant default), mark it **Stale** with the fix and date, don't
  just delete the row — that's the signal to re-run.
