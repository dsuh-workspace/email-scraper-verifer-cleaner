# Email Scraper, Verifier, and Cleaner (HVAC & Plumbing Lead Engine)

A local-first, cloud-ready lead generation pipeline designed to find, clean, verify, and export business leads (specifically tailored for HVAC and plumbing companies) from Google Maps and target websites.

---

## 🚀 Workflow Overview

The pipeline orchestrates five core stages sequentially:

```
Google Maps Scraper (Go/Playwright)
            ↓
  Raw Leads (SQLite Ingestion)
            ↓
Cleaning, Normalization & Deduplication (Base Domain & Name/Phone Matching)
            ↓
Email Harvesting (Website Crawling & Regex Extraction)
            ↓
Google Sheets & CSV Export (Export history logging to prevent spamming)
```

---

## 🛠️ Setup & Installation

### 1. Prerequisites
* **Python 3.10+**
* **Go (Golang)**: Configured in your system path (needed if you want to rebuild the scraper binary).
* **Git**: Installed and configured.

### 2. Install Dependencies
Clone the repository and install the requirements into a Python virtual environment:
```powershell
# Set up virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install required Python packages
pip install -r requirements.txt
```

### 3. Google Maps Scraper Binary
The project uses a compiled Go binary (`google-maps-scraper.exe`) located in `app/scraper/`. 
If you ever need to recompile the binary from source:
```powershell
git clone https://github.com/gosom/google-maps-scraper.git
cd google-maps-scraper
go build -o google-maps-scraper.exe
# Move the compiled google-maps-scraper.exe into app/scraper/
```

---

## ⚙️ Configuration (`.env`)

Create a `.env` file in the project root directory:

```env
DATABASE_URL=sqlite:///hvac_leads.db


# Google Sheets Export: set to 'mock' to fall back to local CSV export
SPREADSHEET_ID=mock
CREDENTIALS_FILE=credentials.json
```

---

## 💻 Usage

### Running the End-to-End Pipeline
To run a complete search, crawl, verification, and export campaign, run:
```powershell
python run_pipeline.py
```

### Customizing Searches
To change search targets, modify the main execution block in [run_pipeline.py](file:///C:/Users/Daniel/hvac-lead-engine/run_pipeline.py):
```python
if __name__ == "__main__":
    run_end_to_end_pipeline(
        query="Plumbing",             # The target industry keyword
        location="San Francisco, CA", # The geographic target (automatically geocoded)
        min_contacts=500              # Scrapes with increasing depth until this contact threshold is met
    )
```

---

## 📂 Project Structure

```
├── app/
│   ├── db/
│   │   ├── create_tables.py   # SQLAlchemy model schemas & initialization
│   │   └── database.py        # Database engine setup & session maker
│   ├── pipeline/
│   │   ├── export_sheets.py   # Google Sheets & local CSV exporter
│   │   ├── extract_emails.py  # Website crawler for email harvesting
│   │   ├── process_leads.py   # Cleaning, normalization, and deduplication
│   └── scraper/
│       ├── google-maps-scraper.exe  # Compiled scraper CLI binary
│       └── run_scraper.py     # Python subprocess wrapper & raw DB loader
├── data/
│   └── leads_export.csv       # Fallback local CSV export destination
├── database/
│   └── hvac_leads.db          # Active SQLite local database file
├── .env                       # Local environment configurations
├── .gitignore                 # Version control exclusions
├── requirements.txt           # Python library dependencies
└── run_pipeline.py            # Master orchestrator script
```

---

## 📊 Database Schema

* **`scrape_runs`**: Logs campaigns, coordinates, search queries, and durations.
* **`raw_leads`**: Temporary landing table where raw scraper API outputs are loaded.
* **`businesses`**: Master cleaned directory of unique businesses (deduplicated by domain or name+phone).
* **`contacts`**: Tracks individuals, titles, and direct contact details, related back to unique businesses.
* **`email_verifications`**: Records detailed SMTP validation logs (scores and status levels).
* **`export_history`**: Tracks contact IDs successfully pushed to Google Sheets or CRM outputs to prevent spam.