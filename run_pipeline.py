import sys
from app.scraper.run_scraper import execute_scrape_and_ingest
from app.pipeline.process_leads import process_and_deduplicate_leads
from app.pipeline.extract_emails import harvest_emails_from_websites
from app.pipeline.export_sheets import export_new_leads

def run_end_to_end_pipeline(query: str, location: str, lat: float, lon: float, depth: int = 1):
    """
    Orchestrates the entire local lead generation pipeline:
    1. Scrapes Google Maps listings & stores raw results.
    2. Cleans & normalizes raw data, moving unique businesses/contacts to tables.
    3. Crawls websites to extract direct email addresses.
    4. Exports newly found leads to Google Sheets (or CSV).
    """
    print("=" * 60)
    print("STARTING END-TO-END LEAD GENERATION PIPELINE")
    print("=" * 60)
    
    try:
        # Step 1: Scraping Google Maps
        execute_scrape_and_ingest(query, location, lat, lon, depth)
        print("-" * 60)
        
        # Step 2: Cleaning & Deduplication
        process_and_deduplicate_leads()
        print("-" * 60)
        
        # Step 3: Crawling Websites for Emails
        harvest_emails_from_websites()
        print("-" * 60)
        
        # Step 4: Exporting Leads
        export_new_leads()
        print("=" * 60)
        print("PIPELINE EXECUTED SUCCESSFULLY")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n[ERROR] Pipeline run aborted: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Default search: Plumbing in Plano, TX
    # Plano, TX coordinates: 33.0198, -96.6989
    run_end_to_end_pipeline(
        query="Plumbing",
        location="Plano, TX",
        lat=33.0198,
        lon=-96.6989,
        depth=1
    )
