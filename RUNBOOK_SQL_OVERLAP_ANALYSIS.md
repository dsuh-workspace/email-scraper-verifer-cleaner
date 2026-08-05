# SQL Runbook: Market Overlap and Lift Analysis

**Purpose**: Manual SQL workflow to measure how much incremental value a new candidate city/batch provides compared to an existing baseline cohort.

**Core principle**: Rely on deduped, first-seen attribution for lift. `raw_leads` contains duplicates and is useful only for run discovery, volume context, and diagnostics. True overlap and lift are calculated from `businesses.first_scrape_run_id` and `contacts.first_scrape_run_id`.

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

Business overlap is easiest to approximate on domain because the canonical `businesses.domain` column is unique when present.

```sql
WITH candidate_raw AS (
  SELECT
    rl.id,
    lower(trim(rl.website)) AS website,
    lower(trim(rl.phone)) AS phone,
    lower(trim(rl.business_name)) AS business_name,
    lower(trim(rl.place_id)) AS place_id,
    lower(trim(rl.domain)) AS domain
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
    ON cr.domain IS NOT NULL
   AND cr.domain <> ''
   AND lower(trim(b.domain)) = cr.domain
)
SELECT
  COUNT(*) FILTER (WHERE business_id IS NOT NULL) AS matched_candidate_raw_rows,
  COUNT(*) FILTER (
    WHERE business_id IS NOT NULL
      AND first_scrape_run_id NOT IN (201, 202, 203, 204)
  ) AS overlap_rows_vs_prior_db,
  COUNT(*) FILTER (
    WHERE business_id IS NOT NULL
      AND first_scrape_run_id IN (201, 202, 203, 204)
  ) AS candidate_first_seen_rows,
  ROUND(
    100.0 * COUNT(*) FILTER (
      WHERE business_id IS NOT NULL
        AND first_scrape_run_id NOT IN (201, 202, 203, 204)
    ) / NULLIF(COUNT(*) FILTER (WHERE business_id IS NOT NULL), 0),
    1
  ) AS overlap_pct_vs_prior_db
FROM matched;
```

This is still a diagnostic view. The source of truth remains first-seen attribution on `businesses` and `contacts`.

## 6. Candidate overlap against the baseline cohort specifically

This asks how much of the candidate raw volume maps to businesses first seen in the baseline cohort.

```sql
WITH candidate_raw AS (
  SELECT
    rl.id,
    lower(trim(rl.domain)) AS domain
  FROM raw_leads rl
  WHERE rl.scrape_run_id IN (201, 202, 203, 204)
    AND rl.domain IS NOT NULL
    AND trim(rl.domain) <> ''
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
  ON lower(trim(b.domain)) = cr.domain;
```

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

Treat that ZIP extraction as approximate only.

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
WITH candidate_domains AS (
  SELECT DISTINCT lower(trim(domain)) AS domain
  FROM raw_leads
  WHERE scrape_run_id IN (201, 202, 203, 204)
    AND domain IS NOT NULL
    AND trim(domain) <> ''
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
JOIN candidate_domains cd ON lower(trim(b.domain)) = cd.domain
JOIN scrape_runs sr ON sr.id = b.first_scrape_run_id
WHERE b.first_scrape_run_id IN (101, 102, 103, 104, 105)
LIMIT 20;
```

## Verification

Before trusting the result:

1. Confirm the baseline and candidate cohorts use explicit run ID lists, not a broad cutoff.
2. Confirm the comparison is one vertical at a time.
3. Confirm business/contact novelty comes from `first_scrape_run_id`, not raw row counts.
4. Confirm export checks join `export_history` on `contact_id`.
5. Confirm verification checks join `email_verifications` on `contact_id`.
6. Spot-check a few rows from both overlap and net-new buckets.

## Interpretation notes

1. **`first_scrape_run_id` is the source of truth.** `process_and_deduplicate_leads()` stamps new `Business` / `Contact` rows from `RawLead.scrape_run_id`. If the first-seen run belongs to the candidate cohort, count it as lift. Otherwise count it as overlap.
2. **`raw_leads` is diagnostic only.** A single business can create many raw rows across queries and passes.
3. **"Exportable" is not the same as "exported".** `export_history` is destination-scoped and contact-scoped.
4. **Verification is optional enrichment.** `email_verifications` can help assess quality, but it is not the primary overlap definition.
5. **Location-level grouping is safer than ZIP parsing from address.** Address-based ZIP extraction is only a fallback heuristic.
