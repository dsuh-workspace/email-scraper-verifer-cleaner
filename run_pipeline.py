import sys
from app.scraper.run_scraper import execute_scrape_and_ingest, geocode_location
from app.pipeline.process_leads import process_and_deduplicate_leads
from app.pipeline.extract_emails import harvest_emails_from_websites
from app.pipeline.export_sheets import export_new_leads

from app.db.database import engine
from sqlalchemy.orm import sessionmaker
from app.db.create_tables import Contact, init_db

def get_contact_count() -> int:
    """
    Counts the total number of contacts in the database.
    """
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        return session.query(Contact).count()
    finally:
        session.close()

def run_end_to_end_pipeline(query: str, location: str, min_contacts: int = 500):
    """
    Orchestrates the entire local lead generation pipeline:
    1. Scrapes Google Maps listings & stores raw results.
    2. Cleans & normalizes raw data, moving unique businesses/contacts to tables.
    3. Crawls websites to extract direct email addresses.
    4. Repeats scraping with increasing depth if target contact count is not met.
    5. Exports newly found leads to Google Sheets (or CSV).
    """
    print("=" * 60)
    print("STARTING END-TO-END LEAD GENERATION PIPELINE")
    print("=" * 60)

    # Bootstrap schema once per run (idempotent — no-op if tables exist).
    init_db()

    # Geocode ONCE up front — Nominatim ToS asks for max 1 req/sec and no
    # duplicate work; we used to re-geocode the same location on every loop
    # iteration.
    lat, lon = geocode_location(location)
    if lat is None or lon is None:
        print(f"[Warning] Could not geocode '{location}'. Scraper will retry per iteration.")

    depth = 1
    max_depth = 20

    try:
        while True:
            print(f"\n--- Running scraping loop (depth={depth}) ---")
            # Step 1: Scraping Google Maps (using cached lat/lon)
            execute_scrape_and_ingest(query, location, lat=lat, lon=lon, depth=depth)
            print("-" * 60)
            
            # Step 2: Cleaning & Deduplication
            process_and_deduplicate_leads()
            print("-" * 60)
            
            # Step 3: Crawling Websites for Emails
            harvest_emails_from_websites()
            print("-" * 60)
            
            current_contacts = get_contact_count()
            print(f"Current contacts count in DB: {current_contacts} / {min_contacts}")
            
            if current_contacts >= min_contacts:
                print(f"\nSuccess! Reached the target of {min_contacts} contacts.")
                break
            
            if depth >= max_depth:
                print(f"\nReached maximum scroll depth ({max_depth}) but did not hit target {min_contacts} contacts. Stopping.")
                break
                
            depth += 2
            print(f"Not enough contacts yet. Increasing scraper depth to {depth} for next iteration...")
            
        # Step 4: Exporting Leads
        export_new_leads()
        print("=" * 60)
        print("PIPELINE EXECUTED SUCCESSFULLY")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n[ERROR] Pipeline run aborted: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Default search: Plumbing in San Francisco, CA
    run_end_to_end_pipeline(
        query="Plumbing",       # Target industry keyword
        location="San Francisco, CA",   # The geographic target
        min_contacts=500         # The minimum contacts target
    )
