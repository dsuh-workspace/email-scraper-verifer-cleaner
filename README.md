# Email Scraper, Verifier, Phone Classifier & Saleshandy Lead Engine

Production-ready, local-first lead generation pipeline for service verticals (**HVAC & Plumbing**). 

Finds, cleans, normalizes, verifies, phone-classifies, and automatically deploys business leads from Google Maps + business websites directly into 12 targeted cold outreach sequences in **Saleshandy** or pre-sorted CSV campaign files.

---

## 🏗️ End-to-End Pipeline Architecture

```
                       1. Google Maps Scraper (Go Binary)
       [single-centroid / grid bbox (--radius-km, --cell-km) / full-harvest]
                                       │
                                       ▼
                       2. Raw Lead Ingestion & Database
                         (SQLite WAL mode / Postgres)
                                       │
                                       ▼
                       3. Clean, Normalize & Deduplicate
                       (Domain + Normalized Name & Phone)
                                       │
                                       ▼
                       4. Async Email Crawler & Extractor
              (Crawl contact/about/team pages + Shared Junk Filters)
                                       │
                                       ▼
                       5. Reacher Deliverability Verification
                           [Opt-in: --verify | min_score]
                                       │
                                       ▼
                     6. Twilio Outbound Phone Classifier & AMD
             (Dispatches 1 call per business; detects IVR, Receptionist,
                Voicemail, Disconnected; propagates status to siblings)
                                       │
                                       ▼
                     7. Saleshandy 12-Permutation Campaign Engine
           (Role email suppression, Business name cleaning, Persona Priority,
              ExportHistory deduplication & direct API push / 12 CSVs)
```

---

## 🚀 Key Features & Architectural Defenses

* **3 Scraping Strategies**:
  * `single-centroid`: Depth loop around location coordinates until target new contacts are reached.
  * `grid`: Bounding box cell tile sweep (`--cell-km 3.0`, `--radius-km 12`) in Playwright JS mode.
  * `full-harvest`: 3-pass strategy combining Grid Pass 1 + Multi-query slow centroid Pass 2 + Fast mode ZIP top-up Pass 3 for maximum market yield.
* **Sticky Proxy Assignment & Block Detection**:
  * Rotates validated proxies from `proxies.txt` using Blake2b hashes of query variants.
  * `app/scraper/block_detect.py` infers soft-blocks from low yield and parks degraded proxies in `data/proxy_health.json`.
* **Timeout Salvaging**:
  * On scraper timeout, partial streamed results are ingested, logs written to `logs/`, and downstream crawling/export continues over salvaged leads.
* **Twilio Phone Classification & Deduplication**:
  * Dispatches **1 call per unique business / phone number** using Twilio Answering Machine Detection (AMD).
  * Automatically classifies phone destinations into **`IVR`**, **`Receptionist`**, **`Voicemail`**, or **`Disconnected`**.
  * Synchronizes classification status across all sibling contacts at the same business.
* **Saleshandy 12-Permutation Campaign Engine**:
  * Classifies leads across 3 dimensions: **Trade** (HVAC/Plumbing) $\times$ **Persona** (Owner/NonOwner) $\times$ **Phone Type** (`IVR`/`Receptionist`/`Voicemail`).
  * **Role Email Suppression**: Automatically excludes complaint-prone prefixes (`careers@`, `billing@`, `jobs@`, `hr@`, `legal@`).
  * **Company Name Cleaning**: Strips legal suffixes (`LLC`, `Inc.`, `Corp.`) and keyword stuffing (`- 24/7 Service`, `| Licensed`).
  * **Persona Priority Rule (`Owner > NonOwner`)**: Ensures each business is enrolled in **exactly one sequence** (5-step Owner OR 3-step NonOwner, never both).
  * **ExportHistory Deduplication**: Prevents duplicate sequence enrollments across pipeline runs.
  * **Resilient API Deployment**: Uses `requests.Session()` with connection pooling and exponential backoff retries for rate limits (HTTP 429) and server errors.

---

## 📊 The 12 Saleshandy Sequence Permutation Matrix

| Permutation Tag | Target Persona | Sequence Type | Phone Destination |
| :--- | :--- | :--- | :--- |
| **`HVAC_Owner_IVR`** | HVAC Owners / Operators | 5-Step (22 Days) | Phone Tree / IVR |
| **`HVAC_Owner_Receptionist`** | HVAC Owners / Operators | 5-Step (22 Days) | Live Receptionist / Front Desk |
| **`HVAC_Owner_Voicemail`** | HVAC Owners / Operators | 5-Step (22 Days) | Answering Machine / Voicemail |
| **`HVAC_NonOwner_IVR`** | HVAC Office / Front Desk | 3-Step (8 Days) | Phone Tree / IVR |
| **`HVAC_NonOwner_Receptionist`** | HVAC Office / Front Desk | 3-Step (8 Days) | Live Receptionist / Front Desk |
| **`HVAC_NonOwner_Voicemail`** | HVAC Office / Front Desk | 3-Step (8 Days) | Answering Machine / Voicemail |
| **`Plumbing_Owner_IVR`** | Plumbing Owners / Operators | 5-Step (22 Days) | Phone Tree / IVR |
| **`Plumbing_Owner_Receptionist`** | Plumbing Owners / Operators | 5-Step (22 Days) | Live Receptionist / Front Desk |
| **`Plumbing_Owner_Voicemail`** | Plumbing Owners / Operators | 5-Step (22 Days) | Answering Machine / Voicemail |
| **`Plumbing_NonOwner_IVR`** | Plumbing Office / Front Desk | 3-Step (8 Days) | Phone Tree / IVR |
| **`Plumbing_NonOwner_Receptionist`** | Plumbing Office / Front Desk | 3-Step (8 Days) | Live Receptionist / Front Desk |
| **`Plumbing_NonOwner_Voicemail`** | Plumbing Office / Front Desk | 3-Step (8 Days) | Answering Machine / Voicemail |

---

## 📦 Setup & Prerequisites

### 1. Requirements
* Python 3.10+ (Recommended `3.12.9`)
* `gosom/google-maps-scraper` binary in `app/scraper/` (`google-maps-scraper.exe` on Windows)

### 2. Environment Configuration (`.env`)
```env
# Database — SQLite for local (WAL mode enabled), Postgres for cloud
DATABASE_URL=sqlite:///database/hvac_leads.db

# Google Sheets export ('mock' falls back to CSV)
SPREADSHEET_ID=mock
CREDENTIALS_FILE=credentials.json

# Reacher email verifier
REACHER_API_URL=http://127.0.0.1:8080/v0/check_email
REACHER_TIMEOUT_SEC=30

# Proxies
SCRAPER_PROXIES_FILE=proxies.txt
CRAWLER_PROXY_FILE=proxies.txt

# Twilio Phone Classifier & AMD Callback
TWILIO_ACCOUNT_SID=your_twilio_account_sid_here
TWILIO_AUTH_TOKEN=your_twilio_auth_token_here
TWILIO_FROM_NUMBER=+14085029426
PUBLIC_BASE_URL=https://your-domain.com

# Saleshandy API & Sequence Mappings
SALESHANDY_API_KEY=your_saleshandy_api_key_here
SH_SEQ_HVAC_OWNER_IVR=your_sequence_id_here
SH_SEQ_HVAC_OWNER_RECEPTIONIST=your_sequence_id_here
SH_SEQ_HVAC_OWNER_VOICEMAIL=your_sequence_id_here
SH_SEQ_HVAC_NONOWNER_IVR=your_sequence_id_here
SH_SEQ_HVAC_NONOWNER_RECEPTIONIST=your_sequence_id_here
SH_SEQ_HVAC_NONOWNER_VOICEMAIL=your_sequence_id_here
SH_SEQ_PLUMBING_OWNER_IVR=your_sequence_id_here
SH_SEQ_PLUMBING_OWNER_RECEPTIONIST=your_sequence_id_here
SH_SEQ_PLUMBING_OWNER_VOICEMAIL=your_sequence_id_here
SH_SEQ_PLUMBING_NONOWNER_IVR=your_sequence_id_here
SH_SEQ_PLUMBING_NONOWNER_RECEPTIONIST=your_sequence_id_here
SH_SEQ_PLUMBING_NONOWNER_VOICEMAIL=your_sequence_id_here
```

---

## 🛠️ CLI Usage & Examples

### Single Location End-to-End Pipeline
```bash
# Run grid sweep for Plumbing in San Jose, verify emails, and deploy to Saleshandy
python run_pipeline.py \
  --query "Plumbing" \
  --location "San Jose, CA" \
  --grid --radius-km 12 --cell-km 3.0 \
  --verify --min-score 50 \
  --saleshandy
```

### Batch ZIP Sweep
```bash
# Run batch ZIP file sweep across multiple ZIP codes and deploy pre-sorted campaigns at the end
python run_zip_batch.py \
  --query "HVAC" \
  --zip-file "san_jose_zips.csv" \
  --grid --cell-km 3.0 \
  --min-score 50 \
  --saleshandy
```

---

## 🧪 Testing & Audits

### Run Full Pytest Suite
```bash
python -m pytest
```

### Audit Sorter Engine against Local Database
```bash
python scripts/audit_sorter.py
```

### Test Saleshandy API Key & Sequences
```bash
python scripts/test_saleshandy_api.py
```
