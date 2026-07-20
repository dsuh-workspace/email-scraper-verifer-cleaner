# Log + DB Analysis — 2026-07-20

## Scope

Reviewed:
- `log_July20.txt`
- `database/hvac_leads.db`
- semantics in `run_pipeline.py` and `app/pipeline/export_sheets.py`

Goal:
- assess log health
- summarize per-ZIP yield
- check DB for suspicious / duplicate emails
- document count/export semantics

---

## Executive summary

Pipeline looks operationally healthy.

- `19` warnings
- `0` errors
- warnings are almost entirely expected stop conditions, not crashes
- batch ZIP run shows strong diminishing returns after first few ZIPs
- DB has suspicious junk emails, but no duplicate non-empty emails
- `new_exportable_contacts` is useful incremental metric
- `total_contacts` is cumulative DB-wide and easy to misread

---

## Log health

### Warning / error trend

`log_July20.txt` produced:
- `19` warnings
- `0` errors

Warnings seen:
- missing `credentials.json` → CSV fallback
- `Reached max scraper depth (9)` for many ZIPs
- `No new exportable contacts for 2 consecutive depth bumps` for dry ZIPs

Interpretation:
- no crash pattern in log
- stop conditions fired as designed
- only real operational warning is missing Google Sheets credentials

---

## Per-ZIP yield

Finished-run lines in log:

| ZIP | Depths | New exportable | Total contacts after ZIP |
|---|---|---:|---:|
| 95110 | `[1]` | 22 | 48 |
| 95111 | `[1, 3, 5, 7, 9]` | 16 | 64 |
| 95112 | `[1, 3, 5, 7, 9]` | 2 | 66 |
| 95113 | `[1, 3]` | 0 | 66 |
| 95116 | `[1, 3, 5, 7, 9]` | 2 | 68 |
| 95117 | `[1, 3, 5, 7, 9]` | 6 | 74 |
| 95118 | `[1, 3, 5, 7, 9]` | 6 | 80 |
| 95119 | `[1, 3, 5, 7, 9]` | 3 | 83 |
| 95120 | `[1, 3]` | 0 | 83 |
| 95121 | `[1, 3, 5, 7, 9]` | 3 | 86 |
| 95122 | `[1, 3]` | 0 | 86 |
| 95123 | `[1, 3]` | 0 | 86 |
| 95124 | `[1, 3, 5, 7, 9]` | 2 | 88 |
| 95125 | `[1, 3, 5, 7, 9]` | 5 | 93 |
| 95126 | `[1, 3, 5, 7, 9]` | 2 | 95 |
| 95127 | `[1, 3, 5, 7, 9]` | 4 | 99 |
| 95128 | `[1, 3]` | 0 | 99 |
| 95129 | `[1, 3, 5, 7, 9]` | 3 | 102 |
| 95130 | `[1, 3]` | 0 | 102 |

### Yield pattern

Strong diminishing returns.

- biggest wins came early: `95110`, `95111`
- many later ZIPs gave only `0-6` new exportable contacts
- several ZIPs stopped after `[1, 3]` because they went stale fast
- deeper runs often kept working but added very little

Interpretation:
- nearby ZIPs overlap heavily
- later ZIPs are mostly incremental cleanup, not step-change growth
- stale-stop logic looks useful and is firing where expected

---

## Zero-yield pattern inside ZIPs

Counts from log parsing:

| ZIP | `Harvested 0 unique email contacts` | `Added 0 new businesses` | `Added 0 new contacts` |
|---|---:|---:|---:|
| 95110 | 0 | 0 | 0 |
| 95111 | 4 | 3 | 3 |
| 95112 | 4 | 4 | 4 |
| 95113 | 2 | 2 | 2 |
| 95116 | 5 | 4 | 4 |
| 95117 | 4 | 4 | 4 |
| 95118 | 5 | 4 | 4 |
| 95119 | 4 | 4 | 4 |
| 95120 | 2 | 2 | 2 |
| 95121 | 4 | 4 | 4 |
| 95122 | 2 | 2 | 2 |
| 95123 | 2 | 2 | 2 |
| 95124 | 5 | 3 | 3 |
| 95125 | 4 | 4 | 4 |
| 95126 | 4 | 4 | 4 |
| 95127 | 4 | 4 | 4 |
| 95128 | 2 | 2 | 2 |
| 95129 | 4 | 4 | 4 |
| 95130 | 2 | 2 | 2 |

Interpretation:
- repeated zero-business / zero-contact / zero-email loops are common after first pass
- this looks like overlap exhaustion, not system breakage
- signal especially strong in `95112`, `95116`, `95118`, `95124`, `95129`

---

## DB snapshot

From `database/hvac_leads.db`:

- `businesses`: observed earlier at `72`
- `contacts`: now `110`
- `raw_leads`: observed earlier at `843`
- `export_history`: `27`

Contact mix:
- contacts with email: `54`
- contacts without email: `56`

Export history:
- only destination seen: `local_csv_leads`
- exported rows logged: `27`

Interpretation:
- about half current contacts still have no email
- export history much smaller than contact table, so many rows remain unexported

---

## Duplicate email check

Query result:
- no duplicate non-empty emails found in `contacts`

Interpretation:
- duplicate-email issue not showing right now
- composite uniqueness and current dedupe path seem to be holding for stored emails

---

## Suspicious email check

Found suspicious / junk-looking emails:

| Business | Email |
|---|---|
| West Water Plumbing | `example@mysite.com` |
| Drain & Plumbing Solutions Llc | `info@mysite.com` |
| Bay Area Plumbing, Rooter & Services | `wilvercasti@gami.com` |

Interpretation:
- crawler is capturing template / placeholder content from some sites
- malformed-looking domains also slip through
- exported outreach list needs spot-checking before use

---

## Count/export semantics

### `run_pipeline.py`

Key logic:
- `get_contact_count()` counts all contacts in DB
- `get_exportable_contact_count(destination)` counts contacts not yet exported for destination
- `new_exportable_contacts = exportable_contacts - baseline_exportable`

Meaning:
- `total_contacts` is cumulative DB-wide count
- `new_exportable_contacts` is per-location incremental delta relative to export state at start of that location run

### `export_sheets.py`

Key logic:
- `export_new_leads()` exports contacts missing `export_history` entry for destination
- destination resolves to `SPREADSHEET_ID` or fallback `local_csv_leads`
- export query does **not** require non-empty email

Meaning:
- “new exportable” means “not yet exported to this destination”
- it does **not** mean:
  - newly harvested email
  - valid email
  - verified email
- phone-only placeholder contacts can still count as exportable and can be exported with blank email

---

## Practical takeaways

1. Batch runner works.
2. Later ZIPs have much lower marginal yield.
3. `new_exportable_contacts` is best batch success metric.
4. `total_contacts` and legacy `--min-contacts` are cumulative and can mislead.
5. Suspicious junk emails already exist in DB.
6. Exported leads should be filtered or spot-checked before outreach.

---

## Follow-up ideas

- filter obvious placeholder emails at ingest / harvest time:
  - `example@*`
  - `*@mysite.com`
  - malformed domains caught by stricter validation
- separate “has email” from “exportable” in reporting
- add batch summary report with per-ZIP marginal yield so weak ZIPs can be skipped earlier
