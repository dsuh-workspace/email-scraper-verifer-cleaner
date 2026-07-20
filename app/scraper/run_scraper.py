import os
import json
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import ScrapeRun, RawLead

from app.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


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


def _validate_proxy_url(proxy_url: str, *, allow_socks: bool) -> str:
    """Trim and validate one proxy URL, returning normalized value."""
    proxy_url = proxy_url.strip()
    if not proxy_url:
        raise ValueError("Proxy URL cannot be empty.")

    parsed = urlparse(proxy_url)
    allowed_schemes = {"http", "https"}
    if allow_socks:
        allowed_schemes.update({"socks5", "socks5h"})

    if parsed.scheme not in allowed_schemes:
        allowed = ", ".join(sorted(allowed_schemes))
        raise ValueError(f"Unsupported proxy scheme {parsed.scheme!r}. Allowed: {allowed}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"Proxy URL must include host and port: {proxy_url!r}")

    return proxy_url


def _load_proxy_file(file_path: str) -> list[str]:
    proxies = []
    with open(file_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            proxies.append(line)
    return proxies


def _scraper_proxy_args() -> list[str]:
    """Return upstream gosom proxy args from env string or file."""
    raw_proxies = os.getenv("SCRAPER_PROXIES", "").strip()
    proxy_file = os.getenv("SCRAPER_PROXIES_FILE", "").strip()

    proxy_values = []
    if raw_proxies:
        proxy_values.extend(raw_proxies.split(","))
    if proxy_file:
        proxy_values.extend(_load_proxy_file(proxy_file))
    if not proxy_values:
        return []

    if any(not proxy_url.strip() for proxy_url in proxy_values):
        raise ValueError(
            "SCRAPER_PROXIES contains an empty entry. Remove trailing or double commas."
        )

    proxies = []
    for proxy_url in proxy_values:
        proxies.append(_validate_proxy_url(proxy_url, allow_socks=True))

    logger.info("Scraper proxies enabled (%d configured).", len(proxies))
    return ["-proxies", ",".join(proxies)]


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
                logger.info(f"Geocoded '{location}' to ({lat}, {lon})")
                return lat, lon
    except Exception as e:
        logger.warning("Geocoding failed for %r: %s", location, e)
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
    logger.info(f"[{datetime.now()}] Started Scrape Run #{scrape_run_id} for '{query}' in '{location}'...")
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
        cmd.extend(_scraper_proxy_args())
        
        if lat is not None and lon is not None:
            cmd.extend(["-geo", f"{lat},{lon}"])
        
        logger.info("Executing: %s", " ".join(cmd))
        # Run the scraper.
        # Redirect stdout/stderr to capture runtime diagnostics.
        # Timeout is a hard 30-min ceiling — deeper crawls (depth>10) can
        # legitimately take a while, but we never want a hung Playwright
        # instance to freeze the pipeline forever. Override via
        # SCRAPER_TIMEOUT_SEC env var if needed.
        scraper_timeout = int(os.getenv("SCRAPER_TIMEOUT_SEC", "1800"))
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                check=True,
                timeout=scraper_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "Scraper exceeded timeout of %ds and was killed.",
                scraper_timeout,
            )
            raise
        
        logger.info("Scraper finished running successfully.")
        # 4. Read results and ingest into database
        if os.path.exists(results_file_path) and os.path.getsize(results_file_path) > 0:
            with open(results_file_path, 'r', encoding='utf-8') as rf:
                leads_data = json.load(rf)
            
            logger.info(f"Found {len(leads_data)} raw leads. Ingesting into database...")
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
                logger.info(f"Successfully ingested {len(raw_leads_to_insert)} raw leads into 'raw_leads' table.")
            else:
                logger.info("No raw leads found to ingest.")
        else:
            logger.warning("Scraper output file is empty or missing.")
        # Update ScrapeRun status
        db_run.status = "completed"
        db_run.completed_at = datetime.now(timezone.utc)
        session.commit()
        logger.info(f"[{datetime.now()}] Completed Scrape Run #{scrape_run_id}.")
    except Exception as e:
        logger.error(f"Error during scrape/ingest: {e}")
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
                logger.error(f"Failed to remove temp file {path}: {cleanup_err}")
        session.close()

if __name__ == "__main__":
    setup_logging()
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
        logger.error(f"Pipeline test run failed: {e}")