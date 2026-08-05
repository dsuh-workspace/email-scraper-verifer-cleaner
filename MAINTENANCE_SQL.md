# Maintenance SQL

Hand-run SQL for schema catch-up, provenance backfills, and data hygiene.
`init_db()` handles additive columns automatically (see `CLAUDE.md` → "DB
migration notes"); everything here is deliberately manual because it is
either non-additive or a one-time backfill.

For cohort/lift analysis queries, see `RUNBOOK_SQL_OVERLAP_ANALYSIS.md`.

## Legacy SQLite schema catch-up

For DBs created before the current model definitions. Each statement
errors harmlessly if the column or index already exists.

```sql
ALTER TABLE raw_leads ADD COLUMN processed_at TIMESTAMP;
CREATE UNIQUE INDEX ix_businesses_domain ON businesses(domain);
CREATE UNIQUE INDEX uq_contact_biz_email ON contacts(business_id, email);
UPDATE export_history SET exported_at = CURRENT_TIMESTAMP WHERE exported_at IS NULL;
```

## Backfill contact provenance (pre-2026-08-04 rows)

Crawl-discovered contacts written before 2026-08-04 landed with NULL
`first_scrape_run_id`, so contact-level cohort queries drop exactly the
crawl-sourced emails that matter most. This copies each contact's
provenance down from its business:

```sql
UPDATE contacts
SET first_scrape_run_id = (
    SELECT b.first_scrape_run_id FROM businesses b WHERE b.id = contacts.business_id
)
WHERE first_scrape_run_id IS NULL;
```

Caveat: this attributes a crawled contact to the run that first found its
*business*, which is a floor rather than the true discovery run. Good
enough for cohort bucketing when the business and the crawl fall in the
same cohort; wrong if the business was found in an earlier cohort than the
crawl that produced the email.

## Backfill business provenance from raw leads

Legacy `businesses` rows predating provenance tracking have NULL
`first_scrape_run_id` and were never backfilled, which is why cohort
queries still need an inference fallback (see `CLAUDE.md` → Open work).

```sql
UPDATE businesses
SET first_scrape_run_id = (
    SELECT MIN(rl.scrape_run_id)
    FROM raw_leads rl
    WHERE rl.business_name = businesses.business_name
)
WHERE first_scrape_run_id IS NULL;
```

Verify the match rate before trusting it — `business_name` is not the
pipeline's dedupe key (base domain is, with `business_name` + E164 phone
as fallback), so this is an approximation. Run the contact backfill above
*after* this one so contacts inherit the filled-in values.

## Legacy bad-domain check

Rows where URL parsing left a bare scheme behind:

```sql
SELECT id, business_name, domain
FROM businesses
WHERE domain = 'http:' OR domain LIKE 'http:%';
```

## Elsewhere

- Force a full re-crawl (clear the crawl-attempt ledger) — `README.md` →
  "Crawl-attempt notes".
- Cohort discovery, lift tables, overlap measurement —
  `RUNBOOK_SQL_OVERLAP_ANALYSIS.md`.
