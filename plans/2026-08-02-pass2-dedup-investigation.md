# Pass 2 Combined-Query Underperformance — 2026-08-02

## Symptom

Full-harvest Pass 2 (multi-query slow sweep at centroid) dramatically
underperforms running the same query variants as separate invocations. On SJ
HVAC (8 variants): **4 raw leads combined vs 81 separate.**

## Root cause

Root-caused via the upstream Go source (`autopilotlocal`'s local clone of
`gosom/google-maps-scraper`, commit `a75a157`).

`filerunner.Run()` (`runner/filerunner/filerunner.go`) creates **one**
`deduper.New()` and **one** `exiter.New()` per process invocation, shared by
every seed job `CreateSeedJobs()` (`runner/jobs.go`) builds from the `-input`
file — i.e. shared across every query variant line when they're all written
to one file for a combined call.

Each seed job scrolls its own feed independently (`GmapJob.BrowserActions`,
`gmaps/job.go`), but when it finds a place `href`, `GmapJob.Process()` calls
`j.Deduper.AddIfNotExists(ctx, href)` against that *shared* instance — a href
already claimed by an earlier-processed variant is silently dropped, never
queued as a `PlaceJob`. Attribution goes non-deterministically to whichever
variant's job claims a href first (scheduling/browser-pool timing), not file
order.

Confirmed this is the upstream tool's correct, intended behavior for its
designed use case (skip re-visiting the same place across grid cells or query
variants) — verified no premature-exit race by tracing `exiter.go`'s atomic
counters and the `PlaceJob`→`EmailJob` completion handoff in `gmaps/place.go`
/ `gmaps/emailjob.go`.

It becomes devastating specifically for Pass 2's use case (near-synonym
queries at the same centroid) because Google Maps returns highly overlapping
business sets for synonym queries in a small radius, so the shared deduper
(working as designed) suppresses nearly all "new" results from
later-processed variants.

## Decision

Fix Python-side, not by patching the vendored Go binary — forking a
well-maintained third party's concurrency-sensitive dedup/exit internals
risks regressing its real primary use case (grid-cell overlap suppression)
and loses future upstream updates, for no benefit the Python-side fix doesn't
already provide.

`--pass2-per-variant` (each variant as its own subprocess → its own fresh
deduper/exiter) is now the **default** for full-harvest; the old combined-call
behavior is opt-in via `--pass2-combined` (`pass2_per_variant=False`), kept
for comparison/diagnostic use.

**Cost:** Pass 2 wall time scales ~Nx (one scrape per variant instead of one
shared-browser-context call) — already quantified as acceptable given the
yield difference.

## Side finding: dead query variants

Same lift-table run also pruned `DEFAULT_HVAC_HARVEST_QUERIES` from 8 → 3
("HVAC", "Heating and cooling", "HVAC contractor") — the other 5 variants
("Air conditioning repair", "Furnace repair", "AC installation", "Heat pump
service", "Ductwork") each contributed ~0 net-new businesses over Pass 1 grid
+ the other variants once run per-variant.

`DEFAULT_HARVEST_QUERIES` (plumbing, still 8 variants) has not had its own
lift-table run — follow-up, see CLAUDE.md TODO.
