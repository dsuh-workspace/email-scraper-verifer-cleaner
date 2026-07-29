# Google Maps Scrape Strategy — Empirical Results (San Jose plumbing, no proxy)

**Date:** 2026-07-20 → 21
**Goal:** Determine best strategy to scrape Google Maps for plumbing leads in San Jose, CA, without proxies.
**Tool:** `gosom/google-maps-scraper` binary (bundled in `app/scraper/`)
**Data key:** `data_id` (unique per business), fallback to `title||address`.

---

## TL;DR

**Winner: Grid + Multi-query + Fast ZIP top-up → 504 unique businesses in ~18 min.**

- 233 have websites (fully email-harvestable)
- Adds 76% more coverage than the current pipeline's single-query centroid call
- Runs comfortably without proxies (single browser context, one Google connection at a time)

Run it via `run_pipeline.py --strategy full-harvest`. The original
standalone runner lives at `scripts/experiments/harvest_best.py`
(offline-only: no DB, no dedupe, no email crawl).

---

## Findings

### 1. Fast-mode caps at ~19 leads per invocation, regardless of `-depth`.

| Mode | Depth | Leads | Wall |
|---|---|---|---|
| fast | 1 | 19 | 2.9s |
| fast | 3 | 19 | 1.9s |
| fast | 10 | 19 | 1.7s |
| **slow** | 1 | 20 | 21s |
| **slow** | 3 | 40 | 41s |
| **slow** | 10 | **110** | 109s |
| slow | 20 | 113 | 103s |

**Depth-10 is the sweet spot in slow mode.** Depth-20 adds only 3 more leads for 2× cost (no; actually same cost, but plateaus).

### 2. Fast and slow return *different* businesses — only ~42% overlap.

Fast-mode reads the **map pin cluster** on initial load. Slow-mode scrolls the **result list**. Different Google surfaces → different sets. Running both is additive.

### 3. Query text variation is a strong lever.

At the SJ centroid, slow d=10, 8 different query variants:

| Query | Leads | New (cumulative) |
|---|---|---|
| Plumbing | 106 | +106 |
| Plumber | 111 | +30 |
| Plumbing services | 110 | +12 |
| Emergency plumber | 111 | +14 |
| Drain cleaning | 111 | +22 |
| Water heater repair | 110 | +25 |
| Leak repair | 112 | +50 ← biggest new lift |
| Sewer service | 101 | +12 |
| **Union** | | **271** |

### 4. Multi-query in **one** input file (Am) is more efficient than separate calls (As).

- 8 separate invocations: 271 unique, 12.6 min → 21.5/min
- 1 invocation with 8 queries: 263 unique, **6.6 min** → 40/min ✅

Scraper internally dedupes and reuses browser context.

### 5. Grid partitioning is the single biggest lever.

Tight SJ bbox (37.20,-121.99 → 37.44,-121.75, ≈27km × 21km) at 3 km cells, slow depth=3, single query "Plumbing":

- **362 unique in 10.1 min**, 72 cells iterated
- Beats every query-variant strategy despite being just ONE query

**Why:** Google Maps ranks by proximity. A centroid query only returns businesses close to that point. Grid re-runs the search from 72 different points, surfacing local businesses in every neighborhood.

### 6. Grid + Query multiplication is NOT additive.

Grid + 6 queries (Dm, aborted at 15%) added only **27 new** businesses over grid+single-query Dt1 (7% marginal). Grid already finds most businesses that Google would return under any plumbing-related query in that geography.

### 7. Grid is nearly deterministic.

Two runs of grid 3km tight: 362 vs 361 leads, **96% overlap**. Repeat runs add ~4%. Not worth doing twice.

### 8. ZIP-centroid sweep (fast mode) — cheap top-up.

28 SJ ZIPs at fast-mode, ~2s each, ~1 min total → 168 unique. Only 168, but at 168/min it's the most efficient per unit time. **78% subset of the grid result** → adds only ~30 net beyond grid, but essentially free.

---

## Strategy Comparison

| Strategy | Time | Unique | Rate | with website |
|---|---:|---:|---:|---:|
| A: 8 fast query variants at centroid | 0.3m | 47 | 178/m | 41 |
| Am: multi-query slow d=10 at centroid | 6.6m | 263 | 40/m | 211 |
| As: 8 separate slow query variants | 12.6m | 271 | 22/m | 215 |
| **Dt: grid 3km tight d=3 + single query** | **10.1m** | **362** | **36/m** | **233** |
| E: 28 ZIP fast d=3 | 1.0m | 168 | 168/m | 130 |
| **Combo: Grid + Am** | **16.7m** | **473** | **28/m** | **326** |
| **Combo: Grid + Am + Fast ZIP** | **17.6m** | **504** | **28/m** | **352** |
| ALL 4 unioned | 30.5m | 531 | 17/m | 373 |

---

## Recommendation

Use **Grid + Multi-query + Fast ZIP top-up**:

```bash
env -u SCRAPER_PROXIES -u SCRAPER_PROXIES_FILE \
    -u CRAWLER_PROXY -u CRAWLER_PROXY_FILE \
    -u CRAWLER_HTTP_PROXY -u CRAWLER_HTTPS_PROXY \
  .venv/bin/python scripts/experiments/harvest_best.py \
    --industry "Plumbing" \
    --bbox "37.20,-121.99,37.44,-121.75" \
    --centroid "37.336,-121.891" \
    --zips-csv san_jose_zips.csv \
    --out data/plumbing_sanjose_best.jsonl
```

For production runs prefer the pipeline equivalent, which also dedupes into
the DB, crawls sites for emails, and exports:

```bash
python run_pipeline.py --query "Plumbing" --location "San Jose, CA" \
  --strategy full-harvest --cell-km 3.0 --zip-csv san_jose_zips.csv
```

Expected: ~500 unique businesses, ~350 with websites, ~18 min wall time.

Feed the JSONL into the existing pipeline's `extract_emails` stage to harvest emails from the ~350 sites.

---

## Why no proxy is fine here

- Single browser instance, `-c 1`, sequential requests
- Total: ~500 GET requests over 18 min = ~30 req/min — well below Google's typical rate-limit trigger
- Playwright's stealth defaults + JS render → looks like normal traffic
- One city per ~week keeps you comfortably below any reputational threshold

Only add proxy if:
- You scale to hitting many cities in the same hour
- You get CAPTCHAs (grid mode fails with `unusual traffic` messages)
- You need faster wall time via `-c > 1`

---

## Nondiscovery notes

Things I did NOT test (recommended follow-up if you want to push past 504):

1. **Wider bbox** — 5km cell on full SJ Nominatim bbox (~1520 km², 60 cells × 40s = 40 min). Would pick up Milpitas/Sunnyvale/Cupertino/Santa Clara plumbers that serve SJ.
2. **Finer cell in dense downtown** — 1 km cells inside 37.32,-121.92 → 37.36,-121.86 (~4km × 4km, ~16 cells) to break the 40-place-per-search cap in dense cells.
3. **Slow ZIP sweep** (Es) — 28 zips × 100s = 47 min. Likely heavy overlap with grid.
4. **Third grid pass at different bbox anchor** — probes non-determinism further.
5. **`-email` flag inside grid** — scraper crawls place websites for emails at scrape-time; would fold Pass 1+2 of the current pipeline into one. Not tested yet — may or may not honor the same fast-mode incompatibility.

---

## Files created

```
scripts/scrape_experiment.py   — experiment harness (one invocation)
scripts/run_experiments.py     — cluster driver (A/As/Am/B/C/D/Dt/Dm/Dr/E/Es)
scripts/analyze_experiments.py — per-experiment stats + overlap matrix
scripts/final_analysis.py      — strategy comparison + combo recommendations
scripts/experiments/harvest_best.py
                              — original standalone runner (Grid + Multi + Fast
                                ZIP). Superseded in production by
                                `run_pipeline.py --strategy full-harvest`.
experiments/*.json             — raw scraper output per experiment
experiments/*.meta.json        — run metadata (queries, geo, depth, wall time)
experiments/*.log              — scraper stdout log (dropped from git)
```

The Nominatim geocoded bbox for SJ (fresh, Jul 20 2026 run): `37.1231596,-122.0462270,37.4691477,-121.5858438`. The tight bbox used above is the populated core.
