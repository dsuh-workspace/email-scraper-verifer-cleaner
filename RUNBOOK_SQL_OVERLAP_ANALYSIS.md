# SQL Runbook: Market Overlap and Lift Analysis

**Purpose**: Manual SQL workflow to measure how much incremental value a new candidate city/batch provides compared to an existing baseline cohort.

**Core principle**: Rely on deduped, first-seen attribution for lift. `raw_leads` contains duplicates and is useful only for run discovery, volume context, and diagnostics. True overlap and lift are calculated from `businesses.first_scrape_run_id` and `contacts.first_scrape_run_id`.

> **Prefer the script.** `scripts/analysis/market_overlap.py` implements this
> whole workflow and handles three traps the raw SQL below does not. Use the SQL
> for ad-hoc slices; use the script for any number you intend to report.
>
> ```bash
> python scripts/analysis/market_overlap.py <db> <candidate_start_id> \
>     --cohort NAME=1-31,33 --cohort OTHER=41-49
> ```
>
> The three traps, all hit for real on the 2026-08-04 Sunnyvale/Santa Clara runs:
>
> 1. **`contacts.first_scrape_run_id` was never stamped by the crawler.** Until
>    the 2026-08-04 fix, only `process_leads.py` stamped it — `extract_emails.py`
>    did not. So every email discovered by website crawling landed with NULL
>    provenance. Any contact-level lift query keyed on that column silently
>    undercounts exactly the crawl-sourced emails you care about (25 of 163 in the
>    HVAC cohort, 22 of 86 in plumbing — all of them with emails). **For data
>    written before 2026-08-04, scope contacts by their business's provenance,
>    not their own.**
> 2. **Legacy businesses have NULL `first_scrape_run_id` too.** Rows created
>    before the column existed were never backfilled, so a plain
>    `first_scrape_run_id < cutoff` predicate drops them from the overlap bucket
>    instead of counting them. All 42 HVAC overlaps were NULL-provenance rows.
>    Fall back to `MIN(raw_leads.scrape_run_id)` for those.
> 3. **Cross-vertical contamination from a shared baseline.** If the candidate DB
>    was seeded from a multi-vertical DB, runs from the *same candidate market*
>    but a *different vertical* can sit below the cohort cutoff and be miscounted
>    as baseline overlap. This inflated the HVAC overlap rate from 12.6% to
>    19.6%. Seed candidate DBs from a **single-vertical** baseline copy.
>
> One more, which is a matching bug rather than a provenance one: the pipeline
> **does not dedupe on `place_id`**. `process_leads.py` keys on base domain, then
> `business_name` + E164 phone. Matching raw leads to businesses on `place_id`
> (as the retired root-level `calculate_overlap.py` did) can group rows
> differently from how the pipeline actually deduped them.

## Context

Use this when deciding whether a candidate market (for example Sunnyvale/Santa Clara) is adding real net-new inventory after a completed baseline market run (for example San Jose).

Use explicit run cohorts, not a global `scrape_run_id >= N` cutoff. This repo often has unrelated later runs, so the comparison should be:
- baseline cohort = specific `scrape_runs.id` list
- candidate cohort = specific `scrape_runs.id` list

## Inputs

- SQLite DB path
- One vertical at a time (HVAC or Plumbing)
- Explicit baseline cohort run IDs
- Explicit candidate cohort run IDs
- Optional export destination when checking exportability (`local_csv_leads` by default)

## 1. Discover cohort run IDs

First inspect recent runs and write down the exact IDs for each cohort.

```sql
SELECT
  id,
  query,
  location,
  category,
  status,
  started_at,
  completed_at
FROM scrape_runs
ORDER BY id DESC
LIMIT 100;
```

Examples:
- baseline cohort = `(101, 102, 103, 104, 105)`
- candidate cohort = `(201, 202, 203, 204)`

Use those explicit lists in the queries below.

## 2. Raw lead volume by cohort

Raw leads are context only. They are not the source of truth for overlap/lift.

```sql
SELECT COUNT(*) AS raw_leads_baseline
FROM raw_leads
WHERE scrape_run_id IN (101, 102, 103, 104, 105);

SELECT COUNT(*) AS raw_leads_candidate
FROM raw_leads
WHERE scrape_run_id IN (201, 202, 203, 204);
```

```sql
SELECT
  sr.location,
  COUNT(*) AS raw_leads
FROM raw_leads rl
JOIN scrape_runs sr ON sr.id = rl.scrape_run_id
WHERE rl.scrape_run_id IN (201, 202, 203, 204)
GROUP BY 1
ORDER BY 2 DESC;
```

## 3. Market-level business lift

Count businesses first introduced by the candidate cohort.

```sql
SELECT COUNT(*) AS net_new_businesses
FROM businesses
WHERE first_scrape_run_id IN (201, 202, 203, 204);
```

```sql
SELECT
  sr.location,
  COUNT(*) AS net_new_businesses
FROM businesses b
JOIN scrape_runs sr ON sr.id = b.first_scrape_run_id
WHERE b.first_scrape_run_id IN (201, 202, 203, 204)
GROUP BY 1
ORDER BY 2 DESC;
```

These are the businesses the dedupe pipeline believes were first introduced by the candidate cohort.

## 4. Market-level contact lift

Count contacts first introduced by the candidate cohort.

```sql
SELECT COUNT(*) AS net_new_contacts
FROM contacts
WHERE first_scrape_run_id IN (201, 202, 203, 204);
```

```sql
SELECT COUNT(*) AS net_new_contacts_with_email
FROM contacts
WHERE first_scrape_run_id IN (201, 202, 203, 204)
  AND email IS NOT NULL
  AND trim(email) <> '';
```

```sql
SELECT
  sr.location,
  COUNT(*) AS net_new_contacts,
  COUNT(*) FILTER (
    WHERE c.email IS NOT NULL AND trim(c.email) <> ''
  ) AS net_new_contacts_with_email
FROM contacts c
JOIN scrape_runs sr ON sr.id = c.first_scrape_run_id
WHERE c.first_scrape_run_id IN (201, 202, 203, 204)
GROUP BY 1
ORDER BY 2 DESC;
```

## 5. Candidate overlap against prior DB state

This estimates how much of the candidate raw volume maps to businesses already known before the candidate cohort.

`raw_leads` has no `domain` column — only `website` (the raw crawled URL, captured at scrape time, before domain extraction). `businesses.domain` is derived later, during website crawling, and only exists on `businesses`. Empirically `businesses.website` stores that same raw URL string verbatim (no business has a populated `website` with a blank `domain`), so match on `website`, not `domain`.

```sql
WITH candidate_raw AS (
  SELECT
    rl.id,
    lower(trim(rl.website)) AS website,
    lower(trim(rl.phone)) AS phone,
    lower(trim(rl.business_name)) AS business_name,
    lower(trim(rl.place_id)) AS place_id
  FROM raw_leads rl
  WHERE rl.scrape_run_id IN (201, 202, 203, 204)
),
matched AS (
  SELECT
    cr.id,
    b.id AS business_id,
    b.first_scrape_run_id
  FROM candidate_raw cr
  LEFT JOIN businesses b
    ON cr.website IS NOT NULL
   AND cr.website <> ''
   AND lower(trim(b.website)) = cr.website
)
SELECT
  COUNT(*) FILTER (WHERE business_id IS NOT NULL) AS matched_candidate_raw_rows,
  COUNT(*) FILTER (
    WHERE business_id IS NOT NULL
      AND (first_scrape_run_id IS NULL OR first_scrape_run_id NOT IN (201, 202, 203, 204))
  ) AS overlap_rows_vs_prior_db,
  COUNT(*) FILTER (
    WHERE business_id IS NOT NULL
      AND first_scrape_run_id IN (201, 202, 203, 204)
  ) AS candidate_first_seen_rows,
  ROUND(
    100.0 * COUNT(*) FILTER (
      WHERE business_id IS NOT NULL
        AND (first_scrape_run_id IS NULL OR first_scrape_run_id NOT IN (201, 202, 203, 204))
    ) / NULLIF(COUNT(*) FILTER (WHERE business_id IS NOT NULL), 0),
    1
  ) AS overlap_pct_vs_prior_db
FROM matched;
```

`first_scrape_run_id` is NULL on legacy rows created before that column existed/was backfilled — treat NULL as prior state (`OR first_scrape_run_id NOT IN (...)` alone silently drops NULL rows from both buckets under SQL's three-valued logic, understating overlap). Check how common this is first:

```sql
SELECT COUNT(*) AS total, COUNT(first_scrape_run_id) AS has_first_scrape_run_id
FROM businesses;
```

This is still a diagnostic view. The source of truth remains first-seen attribution on `businesses` and `contacts`.

## 6. Candidate overlap against the baseline cohort specifically

This asks how much of the candidate raw volume maps to businesses first seen in the baseline cohort.

```sql
WITH candidate_raw AS (
  SELECT
    rl.id,
    lower(trim(rl.website)) AS website
  FROM raw_leads rl
  WHERE rl.scrape_run_id IN (201, 202, 203, 204)
    AND rl.website IS NOT NULL
    AND trim(rl.website) <> ''
)
SELECT
  COUNT(*) FILTER (WHERE b.id IS NOT NULL) AS matched_businesses,
  COUNT(*) FILTER (
    WHERE b.id IS NOT NULL
      AND b.first_scrape_run_id IN (101, 102, 103, 104, 105)
  ) AS overlap_with_baseline,
  ROUND(
    100.0 * COUNT(*) FILTER (
      WHERE b.id IS NOT NULL
        AND b.first_scrape_run_id IN (101, 102, 103, 104, 105)
    ) / NULLIF(COUNT(*) FILTER (WHERE b.id IS NOT NULL), 0),
    1
  ) AS overlap_pct_with_baseline
FROM candidate_raw cr
LEFT JOIN businesses b
  ON lower(trim(b.website)) = cr.website;
```

**This section requires the baseline cohort itself to carry a populated `first_scrape_run_id`.** Unlike section 5 (which can treat NULL as "prior state" and still get a meaningful answer), this query asks a stricter question — overlap with *specifically* the baseline run IDs — and a NULL `first_scrape_run_id` can never match `IN (101, 102, ...)` even when the business really was introduced by that baseline. If the baseline predates the provenance-tracking backfill (common for a long-running or legacy baseline market), `overlap_with_baseline` will silently undercount toward zero. Check baseline coverage before trusting this number:

```sql
SELECT
  COUNT(DISTINCT rl.id) AS baseline_raw_leads,
  COUNT(DISTINCT b.id) FILTER (
    WHERE b.first_scrape_run_id IN (101, 102, 103, 104, 105)
  ) AS baseline_businesses_tagged
FROM raw_leads rl
LEFT JOIN businesses b ON lower(trim(b.website)) = lower(trim(rl.website))
WHERE rl.scrape_run_id IN (101, 102, 103, 104, 105)
  AND rl.website IS NOT NULL AND trim(rl.website) <> '';
```

If `baseline_businesses_tagged` is a small fraction of `baseline_raw_leads`, the baseline predates provenance tracking and `overlap_with_baseline` above is not trustworthy.

If tagged coverage is low, fall back to section 5's broader (NULL-inclusive) framing, or the spot-check in section 10.

## 7. ZIP/location-level yield breakdown

Prefer grouping by `scrape_runs.location` because it comes directly from the batch input. Avoid extracting ZIP from free-text address unless you only need a rough fallback.

```sql
SELECT
  sr.location,
  COUNT(DISTINCT b.id) AS net_new_businesses,
  COUNT(DISTINCT c.id) FILTER (
    WHERE c.email IS NOT NULL AND trim(c.email) <> ''
  ) AS net_new_exportable_contacts
FROM scrape_runs sr
LEFT JOIN businesses b
  ON b.first_scrape_run_id = sr.id
LEFT JOIN contacts c
  ON c.first_scrape_run_id = sr.id
WHERE sr.id IN (201, 202, 203, 204)
GROUP BY 1
ORDER BY net_new_exportable_contacts DESC, net_new_businesses DESC;
```

Approximate ZIP extraction fallback:

```sql
SELECT
  substr(address, -5) AS extracted_zip,
  COUNT(*) AS businesses
FROM businesses
WHERE first_scrape_run_id IN (201, 202, 203, 204)
  AND address IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC;
```

Treat that ZIP extraction as approximate only — in practice, on real address strings, this is wrong more often than not. Addresses commonly end in `, United States` or `, USA`, so `substr(address, -5)` frequently returns literal `tates` (or a trailing `, USA` fragment) instead of a ZIP, and blank-address rows (`address = ''`, which passes the `IS NOT NULL` filter) return an empty string. Both showed up as the single largest "ZIP" bucket in spot checks against real data — bigger than any real ZIP. Group by `scrape_runs.location` (the query above) for anything that needs to be trustworthy; only reach for this fallback when a human will eyeball the output.

## 8. Approximate new exportable contacts

The pipeline's exportability logic is contact-level, not business-level. For manual post-run SQL, approximate exportable contacts as contacts that:
- belong to the candidate cohort
- have a non-blank email
- do not already appear in `export_history` for the destination

```sql
SELECT COUNT(*) AS candidate_new_exportable_contacts
FROM contacts c
WHERE c.first_scrape_run_id IN (201, 202, 203, 204)
  AND c.email IS NOT NULL
  AND trim(c.email) <> ''
  AND NOT EXISTS (
    SELECT 1
    FROM export_history eh
    WHERE eh.contact_id = c.id
      AND eh.destination = 'local_csv_leads'
  );
```

```sql
SELECT
  sr.location,
  COUNT(*) AS candidate_new_exportable_contacts
FROM contacts c
JOIN scrape_runs sr ON sr.id = c.first_scrape_run_id
WHERE c.first_scrape_run_id IN (201, 202, 203, 204)
  AND c.email IS NOT NULL
  AND trim(c.email) <> ''
  AND NOT EXISTS (
    SELECT 1
    FROM export_history eh
    WHERE eh.contact_id = c.id
      AND eh.destination = 'local_csv_leads'
  )
GROUP BY 1
ORDER BY 2 DESC;
```

## 9. Optional verification quality slice

`email_verifications` is keyed by `contact_id`. Join on that key, not on the email string.

```sql
WITH latest_verification AS (
  SELECT
    ev.contact_id,
    MAX(ev.id) AS latest_id
  FROM email_verifications ev
  GROUP BY ev.contact_id
)
SELECT
  COUNT(*) AS candidate_verified_contacts_50_plus
FROM contacts c
JOIN latest_verification lv ON lv.contact_id = c.id
JOIN email_verifications ev ON ev.id = lv.latest_id
WHERE c.first_scrape_run_id IN (201, 202, 203, 204)
  AND c.email IS NOT NULL
  AND trim(c.email) <> ''
  AND ev.score >= 50;
```

Use `>= 95` for safe-only.

## 10. Spot-check diagnostics

Use samples from both buckets to verify the attribution is believable.

```sql
SELECT
  b.id,
  b.business_name,
  b.domain,
  b.phone,
  b.address,
  b.place_id,
  sr.location AS first_seen_location,
  sr.query AS first_seen_query
FROM businesses b
JOIN scrape_runs sr ON sr.id = b.first_scrape_run_id
WHERE b.first_scrape_run_id IN (201, 202, 203, 204)
ORDER BY b.id DESC
LIMIT 20;
```

```sql
WITH candidate_websites AS (
  SELECT DISTINCT lower(trim(website)) AS website
  FROM raw_leads
  WHERE scrape_run_id IN (201, 202, 203, 204)
    AND website IS NOT NULL
    AND trim(website) <> ''
)
SELECT
  b.id,
  b.business_name,
  b.domain,
  b.phone,
  b.address,
  b.place_id,
  b.first_scrape_run_id,
  sr.location AS first_seen_location
FROM businesses b
JOIN candidate_websites cw ON lower(trim(b.website)) = cw.website
JOIN scrape_runs sr ON sr.id = b.first_scrape_run_id
WHERE b.first_scrape_run_id IN (101, 102, 103, 104, 105)
LIMIT 20;
```

(`raw_leads` has no `domain` column — see the note in section 5. And per section 6's caveat, this returns rows only where the matched business already carries a baseline `first_scrape_run_id`; on a legacy baseline it may return nothing even when real overlap exists.)

## Verification

Before trusting the result:

1. Confirm the baseline and candidate cohorts use explicit run ID lists, not a broad cutoff.
2. Confirm the comparison is one vertical at a time.
3. Confirm business/contact novelty comes from `first_scrape_run_id`, not raw row counts.
4. Confirm export checks join `export_history` on `contact_id`.
5. Confirm verification checks join `email_verifications` on `contact_id`.
6. Spot-check a few rows from both overlap and net-new buckets.

## Interpretation notes

1. **`first_scrape_run_id` is the source of truth — with two documented gaps.** `process_and_deduplicate_leads()` stamps new `Business` / `Contact` rows from `RawLead.scrape_run_id`. If the first-seen run belongs to the candidate cohort, count it as lift. Otherwise count it as overlap. The gaps: rows predating the column are NULL and were never backfilled, and contacts created by the *crawler* were not stamped at all before 2026-08-04. See the callout at the top.
2. **`raw_leads` is diagnostic only.** A single business can create many raw rows across queries and passes. The one legitimate analytical use is recovering first-seen for NULL-provenance rows via `MIN(scrape_run_id)`.
3. **"Exportable" is not the same as "exported".** `export_history` is destination-scoped and contact-scoped.
4. **Verification is optional enrichment.** `email_verifications` can help assess quality, but it is not the primary overlap definition.
5. **Location-level grouping is safer than ZIP parsing from address.** Address-based ZIP extraction is only a fallback heuristic. Expect genuine geographic spillover either way — a ZIP-centroid scrape at Santa Clara 95050 legitimately returns adjacent San Jose, Milpitas, Mountain View, and Cupertino businesses. Those are leads, not contamination.
6. **Check per-ZIP *variant* coverage, not just "was this ZIP touched".** A ZIP is only complete when pass-1 grid plus one pass-2 run per query variant have all finished (HVAC 3 variants, plumbing 2). On the 2026-08-04 HVAC cohort, 3 ZIPs had runs but only **one** was variant-complete — counting touched ZIPs overstated coverage by 3×.

   ```sql
   SELECT location, query, status, COUNT(*)
   FROM scrape_runs WHERE id >= :cohort_start
   GROUP BY 1, 2, 3 ORDER BY 1, 2;
   ```

7. **A hard-killed run stays `status = 'running'` forever.** `execute_scrape_and_ingest()` sets `failed` in its exception handler, but SIGKILL / network-stack death / a closed terminal never reaches it. Rows left at `running` are dead processes, not live work. Mark them `interrupted` before analysis, and treat an interrupted cohort's lift as a floor. Cross-check `scripts/analysis/run_wallclock.py`, which excludes NULL `completed_at` rows.
8. **Union overlapping run intervals before quoting a duration, and never compare a concurrent cohort to a sequential one.** `run_wallclock.py` merges intervals, so its output is the elapsed active envelope, not compute cost. The 2026-08-04 HVAC cohort's 212 min came from 3 concurrent processes sharing one DB; plumbing's 116 min was single-process. Those two numbers are not comparable as throughput.
9. **`export_new_leads()` has no run-cohort filter.** It emits every contact absent from `export_history` for the destination — on a DB carrying a baseline, that is the whole DB. Use `scripts/analysis/export_cohort.py` for a cohort-scoped, side-effect-free CSV. See `data/archive/MISLABELED_wholedb_export_2026-08-04_README.md` for what happens otherwise.
