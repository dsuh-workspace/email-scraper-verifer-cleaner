import os
import json
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import ScrapeRun, RawLead


def _scraper_binary_path() -> str:
    """
    Resolve the google-maps-scraper binary path for the current OS.
    Windows: google-maps-scraper.exe
    macOS/Linux: google-maps-scraper (must be chmod +x)
    """
    scraper_dir = os.path.dirname(__file__)
    if platform.system() == "Windows":
        name = "google-maps-scraper.exe"
    else:
        name = "google-maps-scraper"
    return os.path.abspath(os.path.join(scraper_dir, name))

Session = sessionmaker(bind=engine)

def geocode_location(location: str):
    """
    Geocodes a location string using Nominatim OpenStreetMap API.
    Returns (lat, lon) or (None, None) if not found or on error.
    """
    import requests
    headers = {
        "User-Agent": "hvac-lead-engine-scraper/1.0"
    }
    url = f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(location)}&format=json&limit=1"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                print(f"Geocoded '{location}' to ({lat}, {lon})")
                return lat, lon
    except Exception as e:
        print(f"[Warning] Geocoding failed for '{location}': {e}")
    return None, None

def execute_scrape_and_ingest(query: str, location: str, lat: float = None, lon: float = None, depth: int = 1):
    """
    Runs the google-maps-scraper executable for a query at the specified coordinates,
    then parses the resulting JSON and ingests it into the SQLite database.
    """
    session = Session()
    
    # 1. Create a new ScrapeRun entry
    db_run = ScrapeRun(
        query=query,
        location=location,
        category="HVAC/Plumbing",  # Default category context
        status="running",
        started_at=datetime.now(timezone.utc)
    )
    session.add(db_run)
    session.commit()
    scrape_run_id = db_run.id
    print(f"[{datetime.now()}] Started Scrape Run #{scrape_run_id} for '{query}' in '{location}'...")

    # Geocode if lat/lon are not provided
    if lat is None or lon is None:
        geocoded_lat, geocoded_lon = geocode_location(location)
        if geocoded_lat is not None and geocoded_lon is not None:
            lat, lon = geocoded_lat, geocoded_lon

    # 2. Set up temporary files for query and results
    # Use temporary files so we don't pollute the workspace
    fd_query, query_file_path = tempfile.mkstemp(suffix=".txt", prefix="query_")
    fd_results, results_file_path = tempfile.mkstemp(suffix=".json", prefix="results_")
    
    try:
        # Write query to input file
        with os.fdopen(fd_query, 'w', encoding='utf-8') as qf:
            qf.write(f"{query} in {location}\n")
        
        # Close the results file descriptor so the scraper can write to it
        os.close(fd_results)

        # 3. Build the scraper command
        # Binary path is OS-aware (see _scraper_binary_path)
        binary_path = _scraper_binary_path()

        if not os.path.exists(binary_path):
            raise FileNotFoundError(
                f"Scraper binary not found at {binary_path}. "
                f"Download the google-maps-scraper build for {platform.system()} "
                f"and place it at that path (chmod +x on unix)."
            )

        cmd = [
            binary_path,
            "-input", query_file_path,
            "-results", results_file_path,
            "-json",
            "-depth", str(depth),
            "-pages-per-browser", "2",
            "-fast-mode",
            "-email"
        ]
        
        if lat is not None and lon is not None:
            cmd.extend(["-geo", f"{lat},{lon}"])
        
        print(f"Executing: {' '.join(cmd)}")
        
        # Run the scraper
        # We redirect stdout/stderr to capture any runtime diagnostics
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            check=True
        )
        
        print("Scraper finished running successfully.")

        # 4. Read results and ingest into database
        if os.path.exists(results_file_path) and os.path.getsize(results_file_path) > 0:
            with open(results_file_path, 'r', encoding='utf-8') as rf:
                leads_data = json.load(rf)
            
            print(f"Found {len(leads_data)} raw leads. Ingesting into database...")
            
            raw_leads_to_insert = []
            for item in leads_data:
                # Standardize categories (can be a list or a string depending on scraper schema)
                cats = item.get("categories", [])
                category_str = ", ".join(cats) if isinstance(cats, list) else str(cats)
                
                # Standardize email extraction
                emails = item.get("emails", [])
                email_str = ", ".join(emails) if isinstance(emails, list) else str(emails) if emails else None
                
                lead = RawLead(
                    scrape_run_id=scrape_run_id,
                    business_name=item.get("title"),
                    category=category_str,
                    phone=item.get("phone"),
                    website=item.get("web_site"),
                    email=email_str
                )
                raw_leads_to_insert.append(lead)
            
            if raw_leads_to_insert:
                session.add_all(raw_leads_to_insert)
                session.commit()
                print(f"Successfully ingested {len(raw_leads_to_insert)} raw leads into 'raw_leads' table.")
            else:
                print("No raw leads found to ingest.")
        else:
            print("Warning: Scraper output file is empty or missing.")

        # Update ScrapeRun status
        db_run.status = "completed"
        db_run.completed_at = datetime.now(timezone.utc)
        session.commit()
        print(f"[{datetime.now()}] Completed Scrape Run #{scrape_run_id}.")

    except Exception as e:
        print(f"Error during scrape/ingest: {e}")
        # Log failure status to DB
        db_run.status = "failed"
        db_run.completed_at = datetime.now(timezone.utc)
        session.commit()
        raise e

    finally:
        # Clean up temp files
        for path in (query_file_path, results_file_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as cleanup_err:
                print(f"Failed to remove temp file {path}: {cleanup_err}")
        
        session.close()

if __name__ == "__main__":
    # Test Run: Scrape plumbing leads in Plano, TX
    # Plano, TX coordinates: 33.0198, -96.6989
    try:
        execute_scrape_and_ingest(
            query="Plumbing",
            location="Plano, TX",
            lat=33.0198,
            lon=-96.6989,
            depth=1
        )
    except Exception as e:
        print(f"Pipeline test run failed: {e}")
