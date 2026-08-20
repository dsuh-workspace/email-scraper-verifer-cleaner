---
name: outscraper-pipeline
description: Ingests and processes Outscraper Google Maps XLSX/CSV export files through the complete 7-stage lead enrichment and Saleshandy campaign deployment pipeline (Provenance Ingestion, Deduplication, Tomba Decision-Maker Enrichment, Reacher Deliverability Verification, Twilio Phone Classification, 12-Permutation Bucketing, and Live Saleshandy API Deployment with Global Deduplication). Use whenever the user provides an Outscraper file, mentions Outscraper in Downloads, or requests processing an Outscraper lead batch.
---

# Outscraper Complete Lead Ingestion & Campaign Pipeline

## Overview
When the user provides an Outscraper file (e.g. from `~/Downloads/Outscraper-*.xlsx` or `*.csv`) and asks to run the pipeline, execute the complete 7-stage automated workflow.

---

## The 7-Stage End-to-End Pipeline

1. **Stage 1: Provenance Tracking & Ingest (`ScrapeRun` + `RawLead`)**
   - Automatically parses all rows from the Outscraper `.xlsx` or `.csv` file.
   - Creates a unique `ScrapeRun` record to track cohort provenance (`first_scrape_run_id`).
   - Inserts raw records into the `raw_leads` table.

2. **Stage 2: Deduplication, Junk Filtering & Smart Name Extraction**
   - Runs `process_and_deduplicate_leads()`.
   - Matches existing businesses by **Domain**, then **Name + Phone Number**.
   - Filters out non-contractor listings (Supply stores, trade schools, union locals, postal codes).
   - Applies **Smart Name & Persona Extractor**: analyzes email prefixes (e.g. `frank@...` -> *"Frank"*, `tasos.karoutas@...` -> *"Tasos Karoutas"*, `brianelmore@...` -> *"Brian Elmore"*, `renee@...` -> *"Renee"*) to automatically identify Owners and extract real first/last names rather than generic "Info/Office" placeholders.

3. **Stage 3: Decision-Maker Enrichment (Tomba - 2-Gate Deduplication)**
   - Runs `enrich_businesses_with_tomba(exclude_already_exported=True)`.
   - **Gate 1 (Pre-Tomba):** Skips querying businesses whose domain or ID is already in `export_history` (saves API credits).
   - **Gate 2 (Post-Tomba):** Cross-checks returned emails against global `export_history` and existing DB contacts, applying Owner > Non-Owner persona priority.

4. **Stage 4: Email Deliverability Verification (Local Reacher Engine)**
   - Runs `verify_contacts_emails()`.
   - Prioritizes Tier 1 (Reacher Docker container) -> Tier 2 (In-process Local DNS MX Deliverability, free) -> Tier 3 (Tomba fallback). Scores 0–100 (`safe` $\ge 90$, `risky`, `invalid`).

5. **Stage 5: Automated Phone Classification (Twilio - Net-New Leads Only)**
   - Runs `trigger_twilio_outbound_calls(min_score=80, exclude_already_exported=True)`.
   - Strictly skips dialing any business or contact already exported to Saleshandy.
   - Polls and transcribes greetings via Speech-to-Text (`poll_and_classify_completed_calls`).
   - Classifies phone lines into `IVR`, `Voicemail`, `Receptionist`, or `Disconnected`.

6. **Stage 6: Campaign Sorting & Smart Routing (12 Phone + 4 Direct Permutations)**
   - Runs `export_12_saleshandy_permutations()`.
   - Enforces **Persona Priority** (Owner > NonOwner).
   - Enforces **Smart Sequence Routing**:
     - Connected phone lines route to phone-tailored copy (`IVR`, `Receptionist`, `Voicemail`).
     - Unconnected / unanswered phone lines route to **Direct Outreach Sequences** (`HVAC_Owner_Direct`, `HVAC_NonOwner_Direct`, `Plumbing_Owner_Direct`, `Plumbing_NonOwner_Direct`) without false voicemail mentions.
   - Enforces **Global Deduplication** (`exclude_unexported=True`): strictly excludes any contact or email that has ever been exported to Saleshandy in past runs.
   - Enforces **Single-Trade Locking**: businesses can only enter one trade campaign.

7. **Stage 7: Live Saleshandy API Deployment**
   - Runs `push_to_saleshandy_api()`.
   - Enrolls leads into active Saleshandy sequences with matching trade demo phone lines (`472-244-1040` for HVAC, `661-605-3526` for Plumbing).
   - Omits empty fields to guarantee 100% Saleshandy API schema validation compliance.

---

## Execution Command

To execute the entire pipeline on an Outscraper file:

```bash
# Auto-detect latest Outscraper file in Downloads
python scripts/run_outscraper_pipeline.py

# Or specify explicit file path
python scripts/run_outscraper_pipeline.py "C:\Users\Daniel\Downloads\Outscraper-20260819222442s9543.xlsx"
```

### Options & Flags:
* `--skip-tomba`: Skip Tomba decision-maker search.
* `--skip-verify`: Skip Reacher email verification.
* `--skip-calls`: Skip Twilio outbound test calls.
* `--skip-saleshandy-push`: Export CSVs locally without pushing to live Saleshandy API.
* `--min-score N`: Minimum verification score filter (default: `80`).
