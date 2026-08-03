import logging
import os
import json
import platform
import subprocess
import tempfile
from datetime import datetime, timezone

from geopy.geocoders import Nominatim
from sqlalchemy.orm import sessionmaker

from app.db.database import engine
from app.db.create_tables import ScrapeRun, RawLead
from app.logging_config import setup_logging
from app.proxy_utils import load_proxy_file, normalize_proxy_line, validate_proxy_url

logger = logging.getLogger(__name__)

Session = sessionmaker(bind=engine)

DEFAULT_SCRAPER_PROXY_LIMIT = 3
DEFAULT_SCRAPER_PAGES_PER_BROWSER = 2


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

def _resolve_positive_int(value: int | None, env_name: str) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise ValueError(f"{env_name} must be > 0, got {value}")
    return value


def _env_positive_int(env_name: str) -> int | None:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{env_name} must be an integer, got {raw!r}") from e
    return _resolve_positive_int(value, env_name)


def _scraper_proxy_args(
    disable_proxy: bool = False,
    proxy_limit: int | None = None,
) -> list[str]:
    """Return upstream gosom proxy args from env string or file."""
    if disable_proxy:
        logger.info("Scraper proxies disabled for this run.")
        return []

    limit = _resolve_positive_int(
        proxy_limit
        if proxy_limit is not None
        else (_env_positive_int("SCRAPER_PROXY_LIMIT") or DEFAULT_SCRAPER_PROXY_LIMIT),
        "SCRAPER_PROXY_LIMIT",
    )

    raw_proxies = os.getenv("SCRAPER_PROXIES", "").strip()
    proxy_file = os.getenv("SCRAPER_PROXIES_FILE", "").strip()

    proxy_values = []
    if raw_proxies:
        proxy_values.extend(raw_proxies.split(","))
    if proxy_file:
        proxy_values.extend(load_proxy_file(proxy_file))
    if not proxy_values:
        return []

    if any(not proxy_url.strip() for proxy_url in proxy_values):
        raise ValueError(
            "SCRAPER_PROXIES contains an empty entry. Remove trailing or double commas."
        )

    import random

    proxies = []
    for proxy_url in proxy_values:
        proxies.append(
            validate_proxy_url(
                proxy_url,
                error_prefix="Proxy",
                allowed_schemes={"http", "https", "socks5", "socks5h"},
                unsupported_message="Unsupported proxy scheme",
            )
        )

    random.shuffle(proxies)

    if limit is not None:
        proxies = proxies[:limit]

    logger.info("Scraper proxies enabled (%d configured).", len(proxies))
    return ["-proxies", ",".join(proxies)]


def geocode_location(location: str):
    """
    Geocodes a location string using Nominatim OpenStreetMap API.

    Returns (lat, lon, bbox) where bbox is (min_lat, min_lon, max_lat, max_lon)
    matching the scraper's -grid-bbox arg order. bbox is None if Nominatim
    omitted the boundingbox field for the match. Returns (None, None, None)
    on error or no match.
    """
    try:
        geocoder = Nominatim(user_agent="hvac-lead-engine-scraper/1.0")
        match = geocoder.geocode(location, exactly_one=True)
        if not match:
            return None, None, None

        lat = float(match.latitude)
        lon = float(match.longitude)
        bbox = None
        raw_bbox = getattr(match, "raw", {}).get("boundingbox")
        if raw_bbox and len(raw_bbox) == 4:
            try:
                south, north, west, east = (float(x) for x in raw_bbox)
                bbox = (south, west, north, east)  # min_lat, min_lon, max_lat, max_lon
            except (TypeError, ValueError):
                bbox = None
        logger.info(f"Geocoded '{location}' to ({lat}, {lon}) bbox={bbox}")
        return lat, lon, bbox
    except Exception as e:
        logger.warning("Geocoding failed for %r: %s", location, e)
    return None, None, None

def execute_scrape_and_ingest(
    query: str,
    location: str,
    lat: float = None,
    lon: float = None,
    depth: int = 1,
    bbox: tuple[float, float, float, float] | None = None,
    cell_km: float | None = None,
    disable_proxy: bool = False,
    queries: list[str] | None = None,
    fast_mode: bool | None = None,
    lang: str = "en",
    category: str | None = None,
    concurrency: int | None = None,
    browser_pool_size: int | None = None,
    pages_per_browser: int | None = None,
    proxy_limit: int | None = None,
    disable_page_reuse: bool = False,
):
    """
    Runs the google-maps-scraper executable for a query, then parses the
    resulting JSON and ingests it into the SQLite database.

    Three modes:
    - Single-centroid (default): pass lat/lon (or let it geocode from
      location), scraper uses -geo and -fast-mode. ~10-20 businesses per run.
    - Grid mode: pass bbox=(min_lat, min_lon, max_lat, max_lon) and cell_km.
      Scraper iterates cells internally in JS mode (much richer, 4-5x more
      businesses than a curated ZIP sweep, per 2026-07-20 experiment).
      -fast-mode is dropped; scraper rejects it with -grid-bbox.
    - Multi-query mode: pass `queries=[q1, q2, ...]`. All are written to
      the scraper's -input file (one per line as "{q} in {location}"), so
      the scraper reuses its browser context across queries — faster than
      N separate invocations (Am vs As in the 2026-07-20 experiment: ~2x
      faster). BUT for non-grid (single-centroid `-geo`) mode, coverage is
      NOT equivalent: the scraper shares one deduper/exiter across every
      query line in the file, so near-synonym queries at the same centroid
      silently lose most of their results to whichever variant's feed-parse
      claims a place href first. Verified on SJ HVAC (2026-08-01/02): one
      combined 8-variant call yielded 4 raw leads vs 81 for the same 8
      variants run as separate invocations. run_pipeline.py's full-harvest
      Pass 2 defaults to N separate calls for this reason — see its
      `pass2_per_variant` param and CLAUDE.md. Grid mode's dedup-across-
      cells use of this same mechanism is fine; it's specifically the
      multi-query-at-one-centroid case that's degraded.

    fast_mode override:
      - None (default) => True for single-centroid, False for grid.
      - True/False => explicit; caller responsible for compatibility
        (scraper rejects fast_mode=True + bbox).
    """
    resolved_concurrency = _resolve_positive_int(
        concurrency if concurrency is not None else _env_positive_int("SCRAPER_CONCURRENCY"),
        "SCRAPER_CONCURRENCY",
    )
    resolved_browser_pool_size = _resolve_positive_int(
        browser_pool_size if browser_pool_size is not None else _env_positive_int("SCRAPER_BROWSER_POOL_SIZE"),
        "SCRAPER_BROWSER_POOL_SIZE",
    )
    resolved_pages_per_browser = _resolve_positive_int(
        pages_per_browser if pages_per_browser is not None else _env_positive_int("SCRAPER_PAGES_PER_BROWSER"),
        "SCRAPER_PAGES_PER_BROWSER",
    ) or 2

    session = Session()

    # 1. Create a new ScrapeRun entry
    # Derive category from the query so non-HVAC/non-plumbing runs get
    # labeled correctly. Callers can override with an explicit category=.
    effective_category = category if category else (query.strip() if query else None)
    db_run = ScrapeRun(
        query=query,
        location=location,
        category=effective_category,
        status="running",
        started_at=datetime.now(timezone.utc)
    )
    session.add(db_run)
    session.commit()
    scrape_run_id = db_run.id
    logger.info(f"[{datetime.now()}] Started Scrape Run #{scrape_run_id} for '{query}' in '{location}'...")
    # Geocode if lat/lon are not provided (single-centroid mode fallback).
    # Grid mode uses bbox instead, so lat/lon are optional there.
    if bbox is None and (lat is None or lon is None):
        geocoded_lat, geocoded_lon, _ = geocode_location(location)
        if geocoded_lat is not None and geocoded_lon is not None:
            lat, lon = geocoded_lat, geocoded_lon

    # 2. Set up temporary files for query and results
    # Use temporary files so we don't pollute the workspace
    fd_query, query_file_path = tempfile.mkstemp(suffix=".txt", prefix="query_")
    fd_results, results_file_path = tempfile.mkstemp(suffix=".json", prefix="results_")
    
    try:
        # Resolve query list. Multi-query mode passes several queries in one
        # input file so the scraper reuses its browser context across them.
        query_list = [q for q in (queries or []) if q and q.strip()]
        if not query_list:
            query_list = [query]

        with os.fdopen(fd_query, 'w', encoding='utf-8') as qf:
            for q in query_list:
                qf.write(f"{q} in {location}\n")

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

        # Resolve fast_mode: explicit param wins; else default True unless grid.
        use_fast_mode = fast_mode if fast_mode is not None else (bbox is None)
        if use_fast_mode and bbox is not None:
            raise ValueError(
                "fast_mode=True is incompatible with grid mode (bbox). "
                "Scraper rejects the combination."
            )

        cmd = [
            binary_path,
            "-input", query_file_path,
            "-results", results_file_path,
            "-json",
            "-depth", str(depth),
            "-pages-per-browser", str(resolved_pages_per_browser),
            "-lang", lang,
            "-email",
        ]
        if resolved_concurrency is not None:
            cmd.extend(["-c", str(resolved_concurrency)])
        if resolved_browser_pool_size is not None:
            cmd.extend(["-browser-pool-size", str(resolved_browser_pool_size)])
        if disable_page_reuse:
            cmd.append("-disable-page-reuse")
        if bbox is not None:
            # Grid mode: JS-mode iteration over cell centroids inside the
            # scraper. -fast-mode is incompatible with -grid-bbox.
            min_lat, min_lon, max_lat, max_lon = bbox
            cell = cell_km if cell_km is not None else 2.0
            cmd.extend([
                "-grid-bbox", f"{min_lat},{min_lon},{max_lat},{max_lon}",
                "-grid-cell", str(cell),
            ])
        else:
            # Single-centroid mode.
            if use_fast_mode:
                cmd.append("-fast-mode")
            if lat is not None and lon is not None:
                cmd.extend(["-geo", f"{lat},{lon}"])
        cmd.extend(
            _scraper_proxy_args(
                disable_proxy=disable_proxy,
                proxy_limit=proxy_limit,
            )
        )
        
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
                raw_text = rf.read().strip()
            # Grid mode emits JSONL (one object per line); single-centroid
            # mode emits a JSON array. Handle both.
            leads_data = []
            if raw_text.startswith("["):
                leads_data = json.loads(raw_text)
            else:
                for line in raw_text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        leads_data.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning("Skipping malformed JSONL line: %s", e)

            logger.info(f"Found {len(leads_data)} raw leads. Ingesting into database...")
            raw_leads_to_insert = []
            for item in leads_data:
                # Standardize categories (can be a list or a string depending on scraper schema)
                cats = item.get("categories", [])
                category_str = ", ".join(cats) if isinstance(cats, list) else str(cats)
                
                # Standardize email extraction
                emails = item.get("emails", [])
                email_str = ", ".join(emails) if isinstance(emails, list) else str(emails) if emails else None
                
                # Prefer scraper's per-business categories; fall back to the
                # run-level effective_category (derived from query) so we
                # never stamp the wrong industry on new leads.
                lead = RawLead(
                    scrape_run_id=scrape_run_id,
                    business_name=item.get("title"),
                    category=category_str or effective_category,
                    phone=item.get("phone"),
                    website=item.get("web_site"),
                    email=email_str,
                    review_count=item.get("review_count"),
                    review_rating=item.get("review_rating"),
                    address=item.get("address"),
                    status=item.get("status"),
                    description=item.get("description"),
                    place_id=item.get("place_id")
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