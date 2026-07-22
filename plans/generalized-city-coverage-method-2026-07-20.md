# Generalized City Coverage Method — 2026-07-20

## Goal

Define reusable method for deciding when to scrape:
- whole city first
- ZIP/subregion batches first
- city first with fallback ZIP/subregion coverage

Target: method that works across cities, not only San Jose.

---

## Executive recommendation — REVISED after empirical results

**Original recommendation (city-first) has been INVALIDATED by the n=2 experiment (SJ + SC, 2026-07-20).** City-wide query returns ~10-20 businesses total regardless of depth, capturing only 18% of ZIP-sweep coverage with ~1% marginal gain. `-depth` is a per-query pagination knob, not a coverage lever. See [Empirical investigation](#empirical-investigation).

**New default:** **subregion-first (ZIP or grid), optimized.** City-wide run is an optional cheap seed (~3 min per market for ~1% marginal), not the strategy driver.

Default flow (revised):
1. run subregion targets in priority order (existing ZIP list, or auto-generated grid tiles for markets without ZIP CSV)
2. dedup against DB; short-circuit on cumulative stale streak (existing `stale_iterations` mechanism)
3. optionally follow with a single city-wide seed for the ~1% marginal it captures
4. export

Candidate v1 improvements (see [Revised strategy options](#revised-strategy-options)):

- **A. Optimized ZIP sweep** — cleanest incremental. Keep ZIP-first. Add ZIP priority ordering, cumulative-yield stop.
- **B. Grid tiles** — **NATIVELY SUPPORTED by scraper binary via `-grid-bbox` + `-grid-cell`**. Auto-tiles bbox, iterates cells inside Go binary, dedups internally. Python side needs minimal glue.
- **C. Query-variation multiplier** — orthogonal to geo. Same location × N query variants ("plumbing", "plumber", "emergency plumber") multiplies per-centroid yield.

**Recommended flow**: run a grid experiment on SJ (~1-2 hr) BEFORE picking v1. If grid ≥ ZIP-sweep coverage, v1 = B (dominates A). If grid falls short, v1 = A. Either way, C stacks on top.

---

## Revised strategy options

Post-experiment, three candidate directions. Not mutually exclusive.

### A. Optimized ZIP sweep (incremental, safest — **v1 CHOICE**)

Keep current `run_zip_batch` shape. Improve:
- **ZIP priority ordering** — user-supplied `priority` column (integer, lower = earlier). Enables running high-density ZIPs first.
- **Cumulative-yield stop** — beyond per-ZIP `stale_iterations`, add batch-level stop: if last M ZIPs collectively added < N net-new biz, halt remaining. Prevents 15-ZIP zero-yield tail.
- **Dedup transparency** — log `raw_leads_returned` vs `net_new_biz` per ZIP so operator sees saturation curve, not just totals.
- **ZIP CSV can stay per-market** — pre-curated (e.g. `san_jose_zips.csv`, `los_angeles_zips.csv`).

Pros: minimal code change; matches proven approach; empirically validated (95 biz for SJ).

Cons: requires per-market ZIP CSV curation; doesn't generalize to new cities without prep.

#### A — concrete v1 design

**CSV format** (`{market}_zips.csv`):

```csv
zip,city,state,priority
95112,San Jose,CA,1
95110,San Jose,CA,2
95116,San Jose,CA,3
95111,San Jose,CA,4
95122,San Jose,CA,5
...
```

Rules:
- `priority` int, lower = runs first. Ties broken by CSV row order.
- Missing/blank `priority` → treated as `∞` (runs after all prioritized rows, in CSV order).
- Backward-compat with existing SJ CSV: absent `priority` column = all `∞`, everything runs in CSV order (current behavior).

**New CLI flags on `run_zip_batch.py`:**

```
--cumulative-stop-window M     # default 3   — look at trailing M ZIPs
--cumulative-stop-threshold N  # default 5   — halt if trailing M ZIPs added < N net-new biz cumulatively
--saturation-log               # opt-in flag — print per-ZIP saturation row
```

Backward compat: absent flags = current behavior (no batch-level stop).

**Per-ZIP saturation log line format** (when `--saturation-log`):

```
[ZIP 95112] raw=18 dedupe_in_run=1 net_new_biz=17 cum_net_new=17 stop_reason=target
[ZIP 95110] raw=17 dedupe_in_run=3 net_new_biz=14 cum_net_new=31 stop_reason=target
...
[ZIP 95132] raw=15 dedupe_in_run=15 net_new_biz=0 cum_net_new=95 stop_reason=stale
[BATCH STOP] trailing 3 ZIPs added 2 net-new biz (threshold 5). Halting remaining ZIPs: [95140, 95139, 95138, 95136, 95135].
```

**Data model additions:**

- `LocationRunMetrics` already tracks `new_exportable_contacts`, `stale_iterations`. Add:
  - `raw_leads_seen: int` — sum of raw leads returned by scraper across depth iters
  - `net_new_businesses: int` — biz rows inserted (not dedup'd) during this location run
  - `stop_reason: str` — `"target"` / `"stale"` / `"max_depth"` / `"error"`
- New dataclass `BatchRunMetrics`:
  - `locations_run: tuple[str, ...]`
  - `locations_skipped: tuple[str, ...]` — remaining after cumulative stop
  - `per_location: dict[str, LocationRunMetrics]`
  - `total_net_new_businesses: int`
  - `total_new_exportable_contacts: int`
  - `total_new_exportable_email_contacts: int` — parallel metric excluding blank-email placeholders (see Q8 resolution)
  - `batch_stop_reason: str` — `"exhausted"` / `"cumulative_stop"` / `"error"`

**Cumulative-stop algorithm:**

```
window = deque(maxlen=M)
for zip in sorted_zips_by_priority:
    metrics = run_location_pipeline(zip, ...)
    window.append(metrics.net_new_businesses)
    if len(window) == M and sum(window) < N:
        log_batch_stop(remaining=sorted_zips_by_priority[i+1:])
        break
```

Stop condition fires only after M complete runs, so warmup is safe.

**Q8 mitigation (parallel email-only metric):**

Add helper `get_exportable_email_contact_count(destination)` that filters `email IS NOT NULL AND email != ''`. Return alongside existing `exportable_contacts` in `LocationRunMetrics`. Log both. `--target-new-exportable` still gates on `new_exportable_contacts` (backwards compat); operators can spot placeholder-heavy runs from the parallel number.

**Priority ordering resolution:**

Add small helper `_sort_locations_by_priority(rows: list[dict]) -> list[dict]`. Stable-sort:
- key = `(priority_int_or_inf, csv_row_index)`

Missing `priority` field → `math.inf`. Non-int → warn + treat as `inf`.

**Files affected:**

- `run_zip_batch.py` — add flags, sort by priority, cumulative-stop loop, BatchRunMetrics accumulation, final summary log
- `run_pipeline.py` — extend `LocationRunMetrics` with `raw_leads_seen`, `net_new_businesses`, `stop_reason`; extend `run_location_pipeline` to populate them; add `get_exportable_email_contact_count`
- `tests/test_run_pipeline.py` — priority sort tests, cumulative-stop tests (mock `run_location_pipeline`), backwards-compat tests (absent flags = current behavior)
- `README.md` — document new flags + CSV `priority` column

**Non-goals for v1 A:**

- No grid-tile fallback (option B — separate follow-up)
- No query variants (option C — separate follow-up)
- No auto-ZIP-derivation from city name (needs external ZIP-to-city dataset)

**Ship criteria:**

- SJ existing CSV re-run produces same 95 biz with `--cumulative-stop-*` unset (backwards compat proven)
- SJ existing CSV re-run with reasonable stop thresholds skips ≥5 tail ZIPs while retaining ≥95% of biz coverage (real gain shown)
- All existing 73 tests pass; ≥6 new tests for priority + cumulative-stop + email-only metric

### B. Grid-tile auto-fallback — **NATIVELY SUPPORTED BY SCRAPER**

**Discovery 2026-07-20**: the upstream Go scraper (`github.com/gosom/google-maps-scraper`, mirrored at `apl/tools/google-maps-scraper/`) already implements grid scraping as a first-class feature. Package doc:

> Package grid provides utilities to divide a geographic bounding box into a grid of smaller cells. This is useful for overcoming Google Maps' ~120 results-per-search limit: by splitting a large area into many small cells and issuing one search per cell, you can retrieve far more results.

Installed binary flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `-grid-bbox` | (empty) | `minLat,minLon,maxLat,maxLon` — activates grid mode |
| `-grid-cell` | 1.0 | km per cell |
| `-radius` | 10000 | per-query search radius (m). Explains why single-centroid city runs saturated at ~17 biz — one 10km-radius search around SJ centroid |
| `-zoom` | 15 | Google Maps zoom level |
| `-lang` | en | Google language code |

Recipe from upstream `docs/recipes.md`:

```bash
google-maps-scraper -input query.txt -depth 5 \
  -grid-bbox "52.34,13.09,52.68,13.76" \
  -grid-cell 1.0 \
  -results out.json -json ...
```

**Python integration effort:** minimal.
- `run_scraper.execute_scrape_and_ingest` — accept optional `bbox` + `cell_km`; pass `-grid-bbox` / `-grid-cell` to binary instead of `-geo`.
- `run_pipeline.geocode_location` — extend to also return bounding box (Nominatim returns `boundingbox` field, currently discarded).
- Cell iteration + inner-scraper dedup happens in the Go binary. Python-side dedup (`process_leads` on `raw_leads`) still runs against `place_id`, catches anything the Go deduper misses.

**Pros:**
- Upstream canonical solution — battle-tested
- No per-market ZIP CSV curation
- Bbox derivable from geocoder (Nominatim returns it for free)
- One subprocess invocation vs 30 for ZIP sweep → less overhead, less proxy churn
- Cell size = single knob for coverage/cost tradeoff (1km fine, 4km coarse)
- Works for any city, not just ones with pre-curated ZIP lists

**Cons / unknowns:**
- Cell centroids don't match Google Maps' "logical" area boundaries (ZIPs, districts). May have more overlap waste than curated ZIPs.
- Untested locally at this project — need SJ grid experiment to compare vs 95-biz ZIP-sweep baseline.
- `-radius` default 10km with 1km cells means large overlap per cell. Tuning `-radius` down may or may not help.

**Open Q**: does scraper query `"Plumbing in San Jose"` + grid bbox behave the same as `"Plumbing"` alone + grid bbox? Grid uses lat/lon centroids per cell, so the "in San Jose" text hint may cause GMaps to re-narrow. Recipe examples use just `"plumbers in Austin Texas"` — query string still names the city, so grid + city-in-query is the canonical pattern.

**Suggested empirical test before committing:** run `Plumbing` grid over SJ bbox (roughly `37.21,-122.05,37.47,-121.75`) at `-grid-cell 2.0`, compare biz count to ZIP-sweep 95. Expect ≥95 if grid replaces ZIP; if <70, grid alone insufficient and ZIP-first stays.

### C. Query-variation multiplier (orthogonal)

Independent of geographic strategy. For each scrape target, run N query variants:
- `"Plumbing"`, `"Plumber"`, `"Emergency plumbing"`, `"Water heater repair"`, `"Drain cleaning"`, `"Rooter service"`, etc.
- Multiplies per-centroid yield by N (minus dedup).
- Costs N× more scraper time per target.

Pros: cheap to implement; may recover businesses that only rank under specific query phrasings; stackable with A or B.

Cons: N× cost; diminishing returns per additional variant; requires per-vertical variant list (Plumbing variants ≠ HVAC variants ≠ Roofing variants).

### Recommendation — REVISED after grid discovery

Options were originally ranked A > B because B looked like ground-up work. After discovering B is native to the scraper, recommendation is:

**Run a grid experiment on SJ first.** One command, ~1-2 hours runtime. Result determines v1:

- **If grid ≥ 95 biz on SJ**: v1 = B (grid). Skip A entirely — grid subsumes ZIP sweep's coverage without CSV curation.
- **If 70 ≤ grid < 95 biz**: v1 = A+B. Grid as default coarse pass, ZIP CSV for markets needing extra coverage.
- **If grid < 70 biz**: v1 = A (as originally planned). Grid centroids not as effective as curated ZIPs.

C (query variants) stacks orthogonally in all three cases; opt-in flag.

**Grid experiment spec:**

```
Query: "Plumbing" (matches SJ ZIP-sweep baseline)
BBox: "37.21,-122.05,37.47,-121.75" (rough SJ, ~30km × 25km)
Cell size: 2 km (SJ has ~450 km² → ~113 cells)
Depth: 5 (upstream recipe default for grid mode)
Fresh DB
```

Direct comparison vs `hvac_leads.backup-2026-07-20.db` (95 biz, 28 ZIPs, depth 9).

---

## Empirical investigation

Before committing v1 code, run n=2 experiment to test assumption A1 and size `target_new_exportable`.

### Test matrix

| Test | Market | Topology | DB state | Depth cap | Notes |
|------|--------|----------|----------|-----------|-------|
| SJ-city | San Jose, CA | dense metro | fresh | `--max-depth 9` | matches prior SJ ZIP sweep depth |
| SC-city | Santa Clarita, CA | spread suburb | fresh | `--max-depth 9` | contrast topology, no ZIP baseline exists |

Both use `Plumbing` query (matches existing SJ ZIP sweep so overlap comparison is apples-to-apples).

### Measurement

For each run capture:
- total businesses in DB after run
- total contacts (with email + without)
- depth iterations hit (natural stop vs max-depth stop)
- for SJ only: set intersection between city-run business domains and backed-up ZIP-sweep business domains

### Interpretation rubric

Let `B_city` = biz found by city-wide run, `B_zips` = biz found by prior ZIP sweep (SJ = 95).

| Overlap `|B_city ∩ B_zips| / |B_zips|` | Conclusion |
|------|-----------|
| ≥ 80% | A1 validated — city-first strategy sound. `target_new_exportable_city ≈ 0.8 × B_zips`. |
| 50-80% | A1 partially — city-first useful but fallback ZIPs must be broader than 3-5. |
| < 50% | A1 fails — city query too centroid-biased; keep ZIP-first or add multiple centroids per city. |

Santa Clarita has no ZIP baseline. Its city-run absolute biz count vs SJ's tells whether depth-9 city query saturates similarly across topologies. Optional follow-up: run one Santa Clarita ZIP sweep for A1 comparison.

### Sizing `target_new_exportable`

Currently 20 (ZIP-sweep default). No principled city equivalent yet. After tests:
- if SJ city run yields N biz → typical email-yield-rate ≈ contacts/biz — pick target = fraction (e.g. 0.7×) of typical yield so most runs converge without hitting max-depth
- track post-experiment: fill in [Table below](#target-sizing-outcome) once run

### Runtime cost estimate

Depth cap 9 = up to 5 iterations (depths 1,3,5,7,9). Per-iteration: 1 scraper subprocess (~2-8 min) + concurrent crawl over discovered domains. Expect **30-90 min per city**, longer if Reacher verification wired in. Not free — run overnight if needed.

### Target sizing outcome — RUN COMPLETE, A1 FAILED

**Ran 2026-07-20 16:43-17:13. Same `Plumbing` query, `--max-depth 9`, fresh DB per city.**

| Metric | San Jose | Santa Clarita (run 1) | Santa Clarita (snapshot) |
|--------|----------|----------------------|--------------------------|
| Businesses found (city-wide) | **17** | **12** | **14** |
| Contacts total | 17 | 12 | 14 |
| Contacts w/ email | 7 (41%) | 1 (8%) | 2 (14%) |
| Depths executed | (1, 3, 5, 7, 9) | (1, 3, 5, 7, 9) | (1, 3, 5, 7, 9) |
| Raw leads per depth iter | 17, 17, 17, 17, 17 | 11, 11, 11, 11, 11 | 11, 11, 13, 11, 11 |
| Runtime | 2:38 | ~3:00 | ~2:35 |
| Stop reason | max_depth | max_depth | max_depth |

SC variance across two runs (12 vs 14) suggests Google Maps result ordering drifts across queries — same query at same centroid returned slightly different sets. Doesn't change coverage-ratio conclusion.

**Overlap check (SJ only, ZIP sweep = 95 biz baseline, 84 w/ domain):**

| Set | Count |
|-----|-------|
| Overlap (city ∩ ZIPs) | **15** |
| City-only (net new) | **1** (`ejplumbing.com`) |
| ZIPs-only (missed by city) | **69** |
| City coverage of ZIP data | **15 / 84 = 18%** |
| City marginal gain over ZIPs | **1 / 84 ≈ 1%** |

**Conclusion:** Assumption A1 (city query covers most of ZIP-sweep businesses) **fails badly**. City-wide `Plumbing in San Jose` at `-depth 9` returns 17 businesses total, of which 15 are already discoverable by ZIP sweep. Net new value ≈ 1 business.

**Root cause (empirically confirmed):**
Depth iterations return **essentially the same set** with minor variance likely from proxy rotation / GMaps result-ordering drift:
- SJ (run 2): 17, 17, 17, 17, 17 (identical)
- SC (run 1): 11, 11, 11, 11, 11 (identical, total 12 biz)
- SC (snapshot run): 11, 11, **13**, 11, 11 (one iter added 3 net-new, total 14 biz)

`-depth` does **not** systematically expand result set beyond Google Maps' first-page ~10-20 results for `"{query} in {city}"` from centroid. Any occasional bonus is noise, not signal. Depth is a per-query pagination knob, not a coverage lever.

The ZIP sweep's power was never "more depth" — it was **more distinct centroid+query pairs**, each returning a fresh ~10-20 results, producing 95 unique businesses.

**Strategy implications:**
- `coverage_mode = city-first` as default is a strict downgrade vs ZIP sweep
- No `target_new_exportable_city` value can salvage it — depth cap is a scraper/GMaps property, not tunable
- Retaining a city seed run for "informational" purposes adds ~3 min per market for ~1% marginal coverage. Not worth default. Available as opt-in only.

**Also uncovered during testing:** `extract_emails.py:247` had `NameError: name 'ExportHistory' is not defined` — first SJ run crashed on this. Fixed in-place between run 1 and run 2. Ship the fix in commit before v1.

---

## Why this is best default

> **⚠ STALE (2026-07-20 post-experiment):** This section argues for city-first based on pre-test assumptions. **The n=2 empirical test invalidated the city-first premise.** See [Executive recommendation — REVISED](#executive-recommendation--revised-after-empirical-results) and [Empirical investigation](#empirical-investigation) instead. Kept below as historical reasoning trail.

### What current evidence shows

San Jose ZIP sweep showed:
- big gains early
- then strong diminishing returns
- many later ZIPs produced only `0-6` new exportable contacts
- several ZIPs stopped early after stale iterations

Interpretation:
- adjacent ZIPs overlap heavily
- ZIP-by-ZIP everywhere is often wasteful
- city-only may miss edge cases, but ZIP-only over-fragments discovery

So best default is hybrid:
- broad city discovery first
- narrow fallback only where needed

### Evidence caveat (n=1)

Above interpretation is drawn from **one** San Jose ZIP sweep. Load-bearing assumptions not yet independently verified:

- **A1** — city-wide Google Maps query returns most of the businesses that a ZIP sweep of the same city returns
- **A2** — adjacent ZIPs overlap heavily in every market (not just dense metros)
- **A3** — remaining "edge" businesses can be recovered by 3-5 targeted ZIPs

If A1 fails, city-first is strictly worse than pure ZIP sweep. If A2/A3 fail on spread-out markets (LA sprawl, exurbs, rural), the `max_subregions_per_area` cap of 3-5 will silently under-cover them.

See [Empirical investigation](#empirical-investigation) — n=2 experiment (San Jose dense + Santa Clarita spread-out) to test A1 and estimate `target_new_exportable` sizing.

---

## Decision method

> **⚠ STALE (2026-07-20 post-experiment):** Below assumes city-first is viable. It is not. Retained as historical reasoning; see [Revised strategy options](#revised-strategy-options) for the current direction.

### Default mode: `city-first`

For each market:

1. run city-wide target first
   - example: `San Jose, CA`
2. evaluate `new_exportable_contacts`
3. if city run reaches target, stop market
4. if city run does not reach target, run fallback subregions
5. stop market when aggregate target is reached
6. stop fallback when recent subregions repeatedly add zero

### When to use city-first

Use city-first when:
- market is new or DB is sparse
- nearby ZIPs likely overlap heavily
- goal is fastest seed set for outreach
- no strong evidence city query misses outer business clusters

### When to use subregion-first

Use subregion-first only when:
- user needs strict neighborhood/territory control
- city-wide queries are known to bias toward city center
- city is very large/spread and broad query undercovers edges
- user wants comparative performance by area

This should be exception path, not default path.

---

## Core metric

Primary metric:
- `new_exportable_contacts`

Do **not** use as primary strategy metric:
- cumulative `total_contacts`
- legacy `--min-contacts`

Why:
- `total_contacts` is DB-wide cumulative
- `new_exportable_contacts` is marginal gain for current target relative to baseline export state
- this makes city vs ZIP vs neighborhood runs comparable

---

## V1 heuristic policy

Keep v1 simple and explicit.

### Recommended defaults

- `coverage_mode = city-first`
- fallback trigger: city run misses `target_new_exportable`
- `max_subregions_per_area = 3-5` — **PROVISIONAL, arbitrary. See [empirical investigation](#empirical-investigation).** Revisit after n=2 test. If SC has 10 ZIPs and city run recovers ≥80%, cap of 3-5 fine. If <50%, cap too tight; scale with market size.
- stop fallback after `2` **consecutive** zero-yield subregions (matches existing `stale_iterations_limit=2` in `run_location_pipeline`; keep semantics identical to avoid two knobs meaning same thing)
- always evaluate success with `new_exportable_contacts`

### `target_new_exportable` sizing (open)

Value pending [empirical investigation](#empirical-investigation). Two candidates:

- **flat** — `target = 20` (same as ZIP). Simple; will almost always be hit by a city run, so fallback almost never fires. Effectively `city-only` behavior.
- **scaled by scope** — `target_city = k × target_zip` where k reflects city:ZIP business ratio observed in test. Recommended.

Do not ship v1 with default = 20 for city runs without noting fallback will rarely fire.

### V1 decision algorithm

For each market:

1. run city target
2. if `city_new_exportable >= target_new_exportable`, stop
3. else if no subregions configured, stop
4. else run subregions in priority order
5. after each subregion, update aggregate market `new_exportable_contacts`
6. if aggregate target reached, stop
7. if recent subregions are repeatedly dry, stop

Avoid fancy heuristics in v1:
- no population-based branching
- no geospatial polygon logic
- no hidden scoring system

Simple rules easier to trust and debug.

---

## Reusable implementation core

Best existing primitive already exists in repo:

- `run_pipeline.py:run_location_pipeline()`
- `run_pipeline.py:LocationRunMetrics`
- `run_pipeline.py:get_exportable_contact_count()`

### Role of each

#### `run_location_pipeline()`
Single concrete search target runner.

Examples:
- `San Jose, CA`
- `San Jose, CA 95112`
- `Plano, TX`

It already handles:
- geocode once
- scrape/process/harvest loop
- target-based stop logic
- stale-iteration stop logic
- per-target metrics

This should remain target-agnostic.

#### `LocationRunMetrics`
Reusable per-target scorecard.

Important fields:
- `depths_run`
- `final_depth`
- `total_contacts`
- `exportable_contacts`
- `baseline_exportable_contacts`
- `new_exportable_contacts`
- `stale_iterations`

#### `get_exportable_contact_count()`
Best existing marginal count helper.

It tracks contacts not yet exported to destination, which is much more useful for comparing incremental market coverage than raw DB totals.

---

## Proposed orchestration layer

Add higher-level market-planning layer above single target runs.

### Recommended new concepts

#### `SearchTarget`
One concrete search target.

Fields should include:
- `label`
- `location`
- `kind` (`city`, `zip`, `neighborhood`, `district`)
- `priority`

#### `MarketPlan`
Ordered set of `SearchTarget`s for one market.

Example:
- city target: `San Jose, CA`
- fallback ZIPs:
  - `San Jose, CA 95112`
  - `San Jose, CA 95123`
  - `San Jose, CA 95127`

#### `MarketRunMetrics`
Aggregate metrics across city + fallback targets.

Should include:
- market name
- coverage mode
- targets attempted
- per-target yields
- aggregate `new_exportable_contacts`
- stop reason
- best-performing target

---

## Recommended CLI shape

Add generalized runner:
- `run_area_batch.py`

Keep:
- `run_zip_batch.py` as compatibility wrapper or thin forwarder

### Proposed CLI flags

Core flags:
- `--query`
- `--areas-file`
- `--coverage-mode {city-first, subregion-only, city-only}`
- `--target-new-exportable`
- `--max-depth`
- `--stale-iterations`
- `--max-subregions-per-area`
- `--subregion-trigger {below-target, always, never}`

Optional useful flags:
- `--dry-run-plan`
- `--continue-on-error`
- `--export-destination`

---

## Recommended CSV format

Generalize from ZIP CSV to market-plan CSV.

### Suggested columns

Core identity:
- `market`
- `city`
- `state`
- `location`

Fallback coverage:
- `zip`
- `subregion`
- `subregion_type`
- `priority`

Optional overrides:
- `coverage_mode`
- `target_new_exportable`
- `max_subregions`
- `subregion_trigger`

### Parsing rules

1. if `location` present, use exact string
2. if `city` + `state`, build city target
3. if `zip` + `city` + `state`, build ZIP target
4. group rows by `market`
5. if `market` missing, derive from `city,state`

### Why this format

Works for:
- city-only markets
- city + ZIP fallback plans
- ZIP-only legacy plans
- future neighborhood/district fallback without redesign

---

## Reporting requirements

Current logging is per target only. Add market-level reporting.

### Per-market summary should show

- market name
- coverage mode
- targets attempted
- per-target `new_exportable_contacts`
- aggregate `new_exportable_contacts`
- final stop reason
- best target
- how many fallback targets used

### Why this matters

Without market-level summary:
- hard to compare cities
- hard to know whether city-first was enough
- hard to know whether fallback ZIPs were worth cost

---

## Open design decisions — resolutions

Design-side questions surfaced during review. Not empirical; specify now.

### Q3 — subregion priority ordering

**Rule:** priority = `priority` int column in CSV (lower runs first). Missing → CSV row order. Ties broken by row order.

Rationale: user-controlled, simple, deterministic. No auto-derivation (distance-from-centroid, population) in v1 — deferred until we have data suggesting default heuristic beats manual.

### Q4 — city-run failure handling

**Rule:** if city run raises exception (geocode fail, scraper crash, subprocess timeout):
- log at ERROR with market + traceback
- if `coverage_mode == city-only` → mark market failed, continue batch
- else → attempt fallback subregions in priority order; if all also fail, mark market failed

Rationale: partial coverage > zero coverage. Batch resilience already exists per-location in `run_zip_batch.py` (line 101-103 `except Exception … continue`) — mirror at market level.

### Q5 — coverage-mode semantics

Three explicit modes:

| Mode | City run? | Subregion fallback? | When to use |
|------|-----------|---------------------|-------------|
| `city-first` (default) | yes | yes, if city misses `target_new_exportable` | most markets |
| `city-only` | yes | never | markets where city query is known-good; skip cost of subregion planning |
| `subregion-only` | no | yes (all listed subregions in priority order) | legacy ZIP-sweep parity; markets where city query is known-inadequate |

Precedence: per-row `coverage_mode` column > `--coverage-mode` CLI flag > default `city-first`.

### Q6 — aggregate market metric baseline

`MarketRunMetrics.new_exportable_contacts` = sum of per-target `new_exportable_contacts`.

Each per-target `new_exportable_contacts` is computed against the baseline **at the start of that target**, not at market start. So earlier targets' contributions naturally exclude from later targets — no double-counting, no need for cross-target subtraction.

Consequence: aggregate market number is meaningful ("total new leads this market added") but **not** the same as "if we ran only this market from empty DB." Document explicitly in `MarketRunMetrics` docstring.

### Q7 — CSV precedence

Row parsing rules (evaluated top to bottom, first match wins):

1. If `location` column present and non-empty → build target with `location=<value>`, `subregion_type='free-form'`, use exactly. Ignore `city`/`state`/`zip` on same row.
2. Else if `city` + `state` present and `zip` empty → build **city target**.
3. Else if `city` + `state` + `zip` present → build **subregion target** for market `city, state`.
4. Else if `zip` alone → build subregion target with `location=<zip>`, no market grouping.
5. Else → invalid, skip with warning.

`market` column overrides step-2/3 auto-derivation for grouping only, not for target location string.

### Q8 — placeholder contact leak into `new_exportable_contacts`

**Known bug carried over from ZIP runner.** Per `CLAUDE.md`, `new_exportable_contacts` counts phone-only contacts with blank `email`. So `target_new_exportable = 20` can be hit by 20 blank-email rows and still be "success."

**v1 fix:** introduce parallel metric `new_exportable_email_contacts` (contacts with non-null non-empty `email` and no `export_history` for destination). Log both. `target_new_exportable` semantics unchanged (backwards compat) but reporting surfaces both numbers so operator can spot placeholder-heavy markets.

**v2 (optional):** add `--target-metric {any, email}` flag to switch which one gates fallback. Default `any` for compat.

### Q9 — scraper depth semantics (city vs ZIP)

Scraper binary receives `-depth N` and `-geo lat,lon`. Query line is literal `"{query} in {location}"`. Depth = Google Maps scroll count per query. Depth is **not** a coverage radius — it's a per-query result-collection knob.

Implication: city query at depth 9 collects up to N results near city centroid **from Google Maps' city-level result page**. ZIP query at depth 9 collects up to N results near ZIP centroid **from Google Maps' ZIP-narrowed result page**. Not superset/subset — they are different queries returning overlapping but non-identical sets. Empirical measurement (see [investigation](#empirical-investigation)) is the only way to know overlap ratio.

---

## Backward compatibility

Do not break current ZIP workflow.

### Recommendation

- keep `run_zip_batch.py`
- make it call generalized market runner internally
- preserve old ZIP CSV behavior
- preserve single export at batch end

This reduces migration risk and keeps current docs/scripts usable.

---

## Testing plan

### Parsing / planning tests

Add tests for:
- city-only market row
- city + ZIP fallback rows
- ZIP-only legacy rows
- explicit `location` override
- invalid rows skipped/rejected cleanly

### Orchestration tests

Mock `run_location_pipeline()` and verify:
- city run alone hits target
- city run under target, fallback ZIP closes gap
- multiple fallback ZIPs needed
- dry fallback targets stop early
- aggregate market target stops remaining targets

### Compatibility tests

Verify:
- legacy ZIP batch invocation still works
- old ZIP CSV still parses
- export still runs once at batch end

### Manual verification

Use sample market plan with:
- one city target
- three fallback ZIPs

Check:
- planned order correct
- stop reasons correct
- aggregate summary logs make sense

---

## Concrete implementation sequence

1. keep `run_location_pipeline()` as stable primitive
2. add market/target dataclasses
3. add generalized runner `run_area_batch.py`
4. move ZIP-centric parsing/orchestration into generalized planner
5. keep `run_zip_batch.py` as compatibility wrapper
6. add market-level tests
7. update `README.md` with generalized examples

---

## Critical files

- `run_pipeline.py`
- new `run_area_batch.py`
- `run_zip_batch.py`
- `tests/test_run_pipeline.py`
- `README.md`

---

## Bottom line — REVISED (again, after grid discovery)

**Empirical test (n=2, 2026-07-20) killed city-first.** City-wide `Plumbing in {city}` at `-depth 9` captures ~18% of ZIP-sweep coverage. Root cause: default `-radius 10000m` around single centroid + GMaps' ~120-per-query cap.

**Then discovered**: upstream scraper natively supports grid tiling via `-grid-bbox` + `-grid-cell`. This is the canonical fix for the ~120-per-query cap, per the scraper's own package doc.

New generalized method:

**Empirically-selected geographic partition (curated ZIP subregions, native grid tiles, or both) driven by whichever proves higher coverage in a small SJ grid experiment. Judge each subregion by marginal `new_exportable_contacts` (with parallel `new_exportable_email_contacts` metric to catch placeholder-heavy successes).**

That gives:
- proven coverage (SJ ZIP sweep found 95 biz vs city-run's 17)
- same primitives (`run_location_pipeline`, `run_zip_batch`) — evolve rather than rewrite
- clear v1 = option A (optimized ZIP sweep + priority column + cumulative-yield stop)
- clear follow-ups = option B (grid auto-fallback, needs its own test) and option C (query variants)

### Historical note

Pre-test recommendation was "city-first with conditional fallback." Retained above ([Why this is best default](#why-this-is-best-default), [Decision method](#decision-method)) as stale-marked reasoning for future readers who ask "why not city-first?"
