"""
End-to-end lead-gen pipeline orchestrator.

Stages:
    1. Scrape Google Maps → raw_leads
    2. Clean/dedupe → businesses + contacts
    3. Crawl business websites → email contacts
    4. Loop 1-3 at increasing scraper depth until min_contacts hit
    5. Export new leads to Sheets (or CSV fallback)
"""

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import sessionmaker

from app.db.create_tables import Contact, ExportHistory, init_db
from app.db.database import engine
from app.logging_config import setup_logging
from app.pipeline.export_sheets import export_run_outputs
from app.pipeline.extract_emails import harvest_emails_from_websites
from app.pipeline.process_leads import process_and_deduplicate_leads
from app.pipeline.verify_emails import verify_contacts_emails
from app.scraper.run_scraper import execute_scrape_and_ingest, geocode_location

logger = logging.getLogger(__name__)
Session = sessionmaker(bind=engine)
LEGACY_EXPORT_DESTINATION = "local_csv_leads"

# Single-centroid depth-loop bounds. Only that strategy reads them; grid and
# full-harvest run fixed per-pass depths. Kept as named constants so "did the
# user pass this flag?" is never inferred from comparing against a literal.
DEFAULT_MIN_CONTACTS = 500
DEFAULT_MAX_DEPTH = 20

# Default query variants for full-harvest multi-query pass. Chosen from the
# 2026-07-20 SJ experiment — "Leak repair" alone added 50 unique businesses
# no other query surfaced, so the list favors breadth over redundancy.
DEFAULT_HARVEST_QUERIES = (
    "Plumbing",
    "Plumber",
)

# HVAC variants. Pruned from the original 8-variant breadth-over-redundancy
# set to these 3 per a real per-variant lift table run against SJ HVAC on
# 2026-08-01/02 (--pass2-combined diagnostic): "Air conditioning repair",
# "Furnace repair", "AC installation", "Heat pump service", and "Ductwork"
# each contributed ~0 net-new businesses over Pass 1 grid + the other
# variants — only "Heating and cooling" and "HVAC contractor" surfaced real
# incremental lift. "HVAC" is kept as the anchor/base query. See CLAUDE.md
# ("Pass 2 combined-query underperformance") for the full root-cause writeup.
DEFAULT_HVAC_HARVEST_QUERIES = (
    "HVAC",
    "Heating and cooling",
    "HVAC contractor",
)

# Industry keyword patterns. Regex rather than plain substrings because the
# short HVAC forms need word boundaries — bare "ac" as a substring matches
# "backflow", "vacuum", "surface", etc. "leak" is deliberately absent from the
# plumbing set: "AC leak repair" / "refrigerant leak" are HVAC jobs, so the
# word alone can't decide an industry (see review 2026-07-23 #2).
_PLUMBING_PATTERNS = (
    r"plumb",
    r"drain",
    r"sewer",
    r"septic",
    r"water heater",
    r"\brooter\b",
    r"repipe",
)
_HVAC_PATTERNS = (
    r"hvac",
    r"heating",
    r"cooling",
    r"furnace",
    r"air condition",
    r"heat pump",
    r"boiler",
    r"refrigeration",
    r"mini.?split",
    r"ductwork",
    r"\ba/?c\b",
)
_PLUMBING_RE = re.compile("|".join(_PLUMBING_PATTERNS))
_HVAC_RE = re.compile("|".join(_HVAC_PATTERNS))

def _default_harvest_queries(query: str) -> tuple[str, ...] | None:
    """Pick the built-in multi-query set matching `query`'s vertical.

    Plumbing-ish → DEFAULT_HARVEST_QUERIES.
    HVAC-ish     → DEFAULT_HVAC_HARVEST_QUERIES.
    Neither, or  → None, leaving the caller to decide how loud to be.
    both at once

    One vertical per run is the operating model: a run is "plumbing in San
    Jose" or "HVAC in San Jose", never both, so a query naming both is
    ambiguous rather than a request for a double sweep. Returning None for
    it keeps either vertical's set from silently shadowing the other (which
    is what a first-match-wins chain did) and pushes the choice back to the
    caller.

    Pure lookup: no logging, so the caller that knows the context (which
    pass, whether that pass will even run) owns the user-facing message.

    Raises ValueError on a blank query — a multi-query pass built from an
    empty string would scrape garbage and ingest it as real leads.
    """
    q = (query or "").strip().lower()
    if not q:
        raise ValueError("query must be a non-empty string")
    plumbing = _PLUMBING_RE.search(q) is not None
    hvac = _HVAC_RE.search(q) is not None
    if plumbing and hvac:
        return None
    if plumbing:
        return DEFAULT_HARVEST_QUERIES
    if hvac:
        return DEFAULT_HVAC_HARVEST_QUERIES
    return None


@dataclass(frozen=True)
class LocationRunMetrics:
    depths_run: tuple[int, ...]
    final_depth: int
    total_contacts: int
    exportable_contacts: int
    baseline_exportable_contacts: int
    new_exportable_contacts: int
    stale_iterations: int


def get_contact_count() -> int:
    """Count total contacts in DB."""
    session = Session()
    try:
        return session.query(Contact).count()
    finally:
        session.close()



def get_exportable_contact_count(destination: str = LEGACY_EXPORT_DESTINATION) -> int:
    """Count contacts that have not yet been exported to destination."""
    session = Session()
    try:
        return session.query(Contact).filter(
            Contact.email.isnot(None),
            ~Contact.id.in_(
                session.query(ExportHistory.contact_id).filter(
                    ExportHistory.destination == destination,
                    ExportHistory.contact_id.isnot(None),
                )
            )
        ).count()
    finally:
        session.close()



def run_location_pipeline(
    query: str,
    location: str,
    max_depth: int,
    target_new_exportable: int | None = None,
    stale_iterations_limit: int | None = None,
    export_destination: str = LEGACY_EXPORT_DESTINATION,
    disable_scraper_proxy: bool = False,
    disable_crawler_proxy: bool = False,
    scraper_concurrency: int | None = None,
    scraper_browser_pool_size: int | None = None,
    scraper_pages_per_browser: int | None = None,
    scraper_proxy_limit: int | None = None,
    scraper_disable_page_reuse: bool = False,
    lat: float | None = None,
    lon: float | None = None,
) -> LocationRunMetrics:
    """Run scrape/process/harvest loop for one location and return metrics.

    `target_new_exportable` counts contacts this run made newly exportable —
    not cumulative DB contacts — so re-running against a populated DB still
    scrapes. That requires the email crawl inside the loop: a contact only
    becomes exportable once the crawl finds its address (see CLAUDE.md #R7).

    Pass `lat`/`lon` to reuse a centroid the caller already geocoded; both
    omitted means geocode here (one Nominatim call either way).
    """
    if lat is None or lon is None:
        lat, lon, _bbox = geocode_location(location)
    if lat is None or lon is None:
        logger.warning("Could not geocode %r. Scraper will retry per iteration.", location)

    baseline_exportable = get_exportable_contact_count(export_destination)
    depth = 1
    stale_iterations = 0
    depths_run: list[int] = []

    while True:
        logger.info("--- Running scraping loop (depth=%d) ---", depth)
        depths_run.append(depth)

        execute_scrape_and_ingest(
            query,
            location,
            lat=lat,
            lon=lon,
            depth=depth,
            disable_proxy=disable_scraper_proxy,
            concurrency=scraper_concurrency,
            browser_pool_size=scraper_browser_pool_size,
            pages_per_browser=scraper_pages_per_browser,
            proxy_limit=scraper_proxy_limit,
            disable_page_reuse=scraper_disable_page_reuse,
        )
        process_and_deduplicate_leads()
        harvest_emails_from_websites(disable_proxy=disable_crawler_proxy)

        total_contacts = get_contact_count()
        exportable_contacts = get_exportable_contact_count(export_destination)
        new_exportable_contacts = max(0, exportable_contacts - baseline_exportable)

        logger.info(
            "Location %r now has %d total contacts and %d new exportable contacts.",
            location,
            total_contacts,
            new_exportable_contacts,
        )

        if target_new_exportable is not None and new_exportable_contacts >= target_new_exportable:
            logger.info(
                "Reached target of %d new exportable contacts for %r.",
                target_new_exportable,
                location,
            )
            return LocationRunMetrics(
                depths_run=tuple(depths_run),
                final_depth=depth,
                total_contacts=total_contacts,
                exportable_contacts=exportable_contacts,
                baseline_exportable_contacts=baseline_exportable,
                new_exportable_contacts=new_exportable_contacts,
                stale_iterations=stale_iterations,
            )

        if depth >= max_depth:
            logger.warning(
                "Reached max scraper depth (%d) for %r. Stopping.",
                max_depth,
                location,
            )
            return LocationRunMetrics(
                depths_run=tuple(depths_run),
                final_depth=depth,
                total_contacts=total_contacts,
                exportable_contacts=exportable_contacts,
                baseline_exportable_contacts=baseline_exportable,
                new_exportable_contacts=new_exportable_contacts,
                stale_iterations=stale_iterations,
            )

        if target_new_exportable is not None and stale_iterations_limit is not None:
            if new_exportable_contacts == 0:
                stale_iterations += 1
            else:
                stale_iterations = 0

            if stale_iterations >= stale_iterations_limit:
                logger.warning(
                    "No new exportable contacts for %d consecutive depth bumps at %r. Stopping.",
                    stale_iterations_limit,
                    location,
                )
                return LocationRunMetrics(
                    depths_run=tuple(depths_run),
                    final_depth=depth,
                    total_contacts=total_contacts,
                    exportable_contacts=exportable_contacts,
                    baseline_exportable_contacts=baseline_exportable,
                    new_exportable_contacts=new_exportable_contacts,
                    stale_iterations=stale_iterations,
                )

        depth += 2
        logger.info("Increasing scraper depth to %d.", depth)


def _location_metrics(
    baseline_exportable: int,
    depths_run: tuple[int, ...],
    export_destination: str,
) -> LocationRunMetrics:
    """Snapshot DB counts into LocationRunMetrics for the non-looping strategies.

    Lets grid and full-harvest report the same shape as the depth loop, so a
    batch caller can log one line regardless of strategy.

    `depths_run` is the depths actually handed to the scraper — for these
    strategies a record of the passes, not a loop trace. `stale_iterations` is
    0 by construction: a fixed set of passes has no consecutive zero-yield
    depth bumps to count.
    """
    total_contacts = get_contact_count()
    exportable_contacts = get_exportable_contact_count(export_destination)
    return LocationRunMetrics(
        depths_run=depths_run,
        final_depth=depths_run[-1] if depths_run else 0,
        total_contacts=total_contacts,
        exportable_contacts=exportable_contacts,
        baseline_exportable_contacts=baseline_exportable,
        new_exportable_contacts=max(0, exportable_contacts - baseline_exportable),
        stale_iterations=0,
    )


def run_location_grid(
    query: str,
    location: str,
    bbox: tuple[float, float, float, float] | None,
    cell_km: float = 2.0,
    export_destination: str = LEGACY_EXPORT_DESTINATION,
    disable_scraper_proxy: bool = False,
    disable_crawler_proxy: bool = False,
    scraper_concurrency: int | None = None,
    scraper_browser_pool_size: int | None = None,
    scraper_pages_per_browser: int | None = None,
    scraper_proxy_limit: int | None = None,
    scraper_disable_page_reuse: bool = False,
) -> LocationRunMetrics:
    """Grid-scrape one location's bounding box. One scrape, no depth loop.

    `bbox` is the caller's already-resolved box (Nominatim's, or a `--bbox`
    override), so this geocodes nothing — the centroid alone can't produce
    cells. `None` raises rather than falling back to a centroid scrape, which
    would quietly deliver single-centroid coverage under a grid label.
    """
    if bbox is None:
        raise RuntimeError(
            f"Grid mode requires a bounding box. Nominatim returned none "
            f"for {location!r} and no --bbox override was supplied."
        )

    baseline_exportable = get_exportable_contact_count(export_destination)

    logger.info("--- Grid scrape (bbox=%s cell_km=%.2f) ---", bbox, cell_km)
    execute_scrape_and_ingest(
        query,
        location,
        bbox=bbox,
        cell_km=cell_km,
        depth=3,
        disable_proxy=disable_scraper_proxy,
        concurrency=scraper_concurrency,
        browser_pool_size=scraper_browser_pool_size,
        pages_per_browser=scraper_pages_per_browser,
        proxy_limit=scraper_proxy_limit,
        disable_page_reuse=scraper_disable_page_reuse,
    )
    process_and_deduplicate_leads()
    harvest_emails_from_websites(disable_proxy=disable_crawler_proxy)

    metrics = _location_metrics(baseline_exportable, (3,), export_destination)
    logger.info(
        "Grid scrape complete for %r. Contacts in DB: %d. New exportable: %d.",
        location,
        metrics.total_contacts,
        metrics.new_exportable_contacts,
    )
    return metrics


def run_location_full_harvest(
    query: str,
    location: str,
    bbox: tuple[float, float, float, float] | None,
    lat: float | None = None,
    lon: float | None = None,
    cell_km: float = 2.0,
    queries: tuple[str, ...] | None = None,
    zip_csv: str | None = None,
    export_destination: str = LEGACY_EXPORT_DESTINATION,
    disable_scraper_proxy: bool = False,
    disable_crawler_proxy: bool = False,
    scraper_concurrency: int | None = None,
    scraper_browser_pool_size: int | None = None,
    scraper_pages_per_browser: int | None = None,
    scraper_proxy_limit: int | None = None,
    scraper_disable_page_reuse: bool = False,
    pass2_per_variant: bool = True,
) -> LocationRunMetrics:
    """Grid + multi-query slow at centroid + optional fast ZIP top-up.

    No depth loop. Geo is the caller's: `bbox` drives Pass 1, `lat`/`lon` drive
    Pass 2. A missing bbox raises; a missing centroid only skips Pass 2 with a
    warning, since Pass 1 still produced coverage.

    `queries` overrides Pass 2's variant list. `None` (not an empty tuple)
    means "derive from the query's industry"; `()` is a caller bug and raises.

    `zip_csv` drives the optional Pass 3. Batch callers should leave it None —
    a per-location run inside a ZIP sweep would re-sweep the same ZIPs.

    `pass2_per_variant` (default True): runs each Pass 2 variant as its own
    `execute_scrape_and_ingest` call (one `scrape_runs` row per variant,
    tagged with the variant text) instead of one combined multi-query
    invocation. This is the default because the combined call under-delivers:
    the upstream Go scraper (`gosom/google-maps-scraper`) shares one
    `deduper`/`exiter` pair across every seed job derived from the `-input`
    file when not using `-grid-bbox`, so each query line's newly-found place
    hrefs are silently dropped once a concurrently-scheduled sibling variant's
    feed-parse has already claimed them — regardless of whether that sibling
    "should" get credit. Empirically (SJ HVAC, 2026-08-01/02): one combined
    8-variant call yielded 4 raw leads total; the same 8 variants run
    separately yielded 81. See CLAUDE.md ("Pass 2 combined-query
    underperformance") for the full source-level writeup and why this isn't
    being patched upstream. Costs roughly len(variants)x Pass 2 wall time
    since separate invocations can't reuse one browser context — pass
    `pass2_per_variant=False` (CLI: `--pass2-combined`) to opt back into the
    old combined call for comparison/diagnostic purposes.
    """
    if bbox is None:
        raise RuntimeError(
            f"full-harvest requires a bounding box for the grid pass. "
            f"Nominatim returned none for {location!r}; supply --bbox."
        )
    # `queries is None` (not falsy) means "derive from the query's industry";
    # an explicit empty tuple is a caller bug, so reject it before spending
    # Pass 1 wall time on it.
    if queries is not None and not queries:
        raise ValueError(
            "queries must be None (derive from industry) or a "
            "non-empty sequence of query variants"
        )

    baseline_exportable = get_exportable_contact_count(export_destination)
    depths_run: list[int] = []
    # Pass 2's variant list is resolved inside the Pass 2 guard below, so an
    # unknown industry doesn't warn about a pass that never runs.
    pass2_degraded = False

    # Pass 1 — grid over bbox (single query, JS mode, depth 3).
    logger.info("--- Full-harvest PASS 1: grid (bbox=%s cell_km=%.2f) ---",
                bbox, cell_km)
    execute_scrape_and_ingest(
        query,
        location,
        bbox=bbox,
        cell_km=cell_km,
        depth=3,
        disable_proxy=disable_scraper_proxy,
        concurrency=scraper_concurrency,
        browser_pool_size=scraper_browser_pool_size,
        pages_per_browser=scraper_pages_per_browser,
        proxy_limit=scraper_proxy_limit,
        disable_page_reuse=scraper_disable_page_reuse,
    )
    depths_run.append(3)
    process_and_deduplicate_leads()

    # Pass 2 — multi-query slow at centroid. Per-variant is the default (see
    # docstring): the combined call is faster on wall time but the upstream
    # scraper's shared deduper/exiter across all variants in one -input file
    # silently drops most variants' results, so it under-delivers on yield.
    if lat is not None and lon is not None:
        query_variants = queries
        if query_variants is None:
            query_variants = _default_harvest_queries(query)
            if query_variants is None:
                pass2_degraded = True
                query_variants = (query,)
                logger.warning(
                    "No built-in harvest query set for %r; PASS 2 runs "
                    "the base query alone, so this full-harvest yields "
                    "roughly Pass-1-only coverage at full wall-clock "
                    "cost. Supply --queries \"q1,q2,...\" for the full "
                    "multi-query sweep.",
                    query,
                )
        query_variants = list(query_variants)
        logger.info(
            "--- Full-harvest PASS 2: multi-query slow at centroid "
            "(%d quer%s%s) ---",
            len(query_variants),
            "y" if len(query_variants) == 1 else "ies",
            ", one scrape_runs row per variant" if pass2_per_variant else "",
        )
        if pass2_per_variant:
            # Default: N separate invocations so each variant gets its own
            # scrape_runs row (query=variant text) and, crucially, its own
            # fresh deduper/exiter — the combined call's shared instance is
            # what suppresses cross-variant yield (see docstring). Slower
            # than the combined call below — no shared browser context.
            for variant in query_variants:
                execute_scrape_and_ingest(
                    variant,
                    location,
                    lat=lat,
                    lon=lon,
                    depth=10,
                    fast_mode=False,
                    disable_proxy=disable_scraper_proxy,
                    concurrency=scraper_concurrency,
                    browser_pool_size=scraper_browser_pool_size,
                    pages_per_browser=scraper_pages_per_browser,
                    proxy_limit=scraper_proxy_limit,
                    disable_page_reuse=scraper_disable_page_reuse,
                )
        else:
            # Legacy/diagnostic: one combined multi-query call. Kept for
            # comparison — see docstring for why it under-delivers and is no
            # longer the default.
            execute_scrape_and_ingest(
                query,
                location,
                lat=lat,
                lon=lon,
                depth=10,
                queries=query_variants,
                fast_mode=False,
                disable_proxy=disable_scraper_proxy,
                concurrency=scraper_concurrency,
                browser_pool_size=scraper_browser_pool_size,
                pages_per_browser=scraper_pages_per_browser,
                proxy_limit=scraper_proxy_limit,
                disable_page_reuse=scraper_disable_page_reuse,
            )
        # One entry for the whole pass regardless of mode — consistent with
        # Pass 3's convention (see below): depths_run records passes, not the
        # sub-calls within one.
        depths_run.append(10)
        process_and_deduplicate_leads()
    else:
        logger.warning("Skipping PASS 2 — no centroid available for %r.", location)

    # Pass 3 — fast ZIP top-up (optional). Cheap, ~2s per ZIP.
    if zip_csv:
        logger.info("--- Full-harvest PASS 3: fast ZIP top-up from %s ---", zip_csv)
        zip_rows = _load_zip_csv(zip_csv)
        zip_scrapes = 0
        for i, row in enumerate(zip_rows, 1):
            zip_loc = ", ".join(x for x in (row["city"], row["state"], row["zip"]) if x)
            zlat, zlon, _ = geocode_location(zip_loc) if zip_loc else (None, None, None)
            if zlat is None or zlon is None:
                logger.warning("  [%d/%d zip %s] geocode failed, skipping.",
                               i, len(zip_rows), row["zip"])
                continue
            # Same "[i/N zip Z] ..." prefix as the geocode-failure line
            # above, so one log-parser regex covers both outcomes.
            logger.info("  [%d/%d zip %s] scraping %s",
                        i, len(zip_rows), row["zip"], zip_loc)
            execute_scrape_and_ingest(
                query,
                zip_loc,
                lat=zlat,
                lon=zlon,
                depth=3,
                fast_mode=True,
                disable_proxy=disable_scraper_proxy,
                concurrency=scraper_concurrency,
                browser_pool_size=scraper_browser_pool_size,
                pages_per_browser=scraper_pages_per_browser,
                proxy_limit=scraper_proxy_limit,
                disable_page_reuse=scraper_disable_page_reuse,
            )
            zip_scrapes += 1
        # One entry for the whole pass, not one per ZIP — a 28-ZIP sweep would
        # otherwise bury the pass structure under 28 identical depths.
        if zip_scrapes:
            depths_run.append(3)
        process_and_deduplicate_leads()
    else:
        logger.info("--- Full-harvest PASS 3: skipped (no --zip-csv) ---")

    harvest_emails_from_websites(disable_proxy=disable_crawler_proxy)

    metrics = _location_metrics(
        baseline_exportable, tuple(depths_run), export_destination
    )
    logger.info(
        "Full-harvest complete for %r. Contacts in DB: %d. New exportable: %d.%s",
        location,
        metrics.total_contacts,
        metrics.new_exportable_contacts,
        (
            " NOTE: PASS 2 ran degraded (base query only) — expect "
            "roughly Pass-1-only coverage."
            if pass2_degraded
            else ""
        ),
    )
    return metrics


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run end-to-end lead generation pipeline."
    )
    parser.add_argument("--query", required=True, help="Industry keyword to scrape")
    parser.add_argument(
        "--location", required=True, help="Location string for Google Maps search"
    )
    # --min-contacts / --max-depth default to None rather than their effective
    # values so "user passed the flag" is knowable; run_end_to_end_pipeline
    # resolves None to DEFAULT_MIN_CONTACTS / DEFAULT_MAX_DEPTH.
    parser.add_argument(
        "--min-contacts",
        type=int,
        default=None,
        help=(
            f"Stop once THIS RUN has produced at least this many new "
            f"exportable contacts (default {DEFAULT_MIN_CONTACTS}). Counts "
            f"contacts with an email that are not yet in export_history for "
            f"the destination — not cumulative DB contacts, so re-running "
            f"against a populated DB still scrapes. Must be > 0. "
            f"single-centroid strategy only; ignored by grid/full-harvest."
        ),
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help=(
            f"Maximum scraper depth before stopping (default "
            f"{DEFAULT_MAX_DEPTH}). Must be > 0. single-centroid strategy "
            f"only; ignored by grid/full-harvest, which run fixed per-pass "
            f"depths."
        ),
    )
    parser.add_argument(
        "--grid",
        action="store_true",
        help=(
            "Use native grid-mode scraping (JS-mode, requires Playwright). "
            "One scrape iterates cells over the location's bounding box. "
            "Empirically 4-25x higher coverage than single-centroid mode."
        ),
    )
    parser.add_argument(
        "--cell-km",
        type=float,
        default=2.0,
        help="Grid cell size in km (default 2.0). Ignored without --grid.",
    )
    parser.add_argument(
        "--bbox",
        type=str,
        default=None,
        help=(
            "Explicit bounding box 'min_lat,min_lon,max_lat,max_lon' for "
            "grid mode. Overrides Nominatim-derived bbox. Ignored without --grid."
        ),
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable scraper and crawler proxy usage for this run.",
    )
    parser.add_argument(
        "--no-scraper-proxy",
        action="store_true",
        help="Disable scraper proxy usage for this run.",
    )
    parser.add_argument(
        "--no-crawler-proxy",
        action="store_true",
        help="Disable crawler proxy usage for this run.",
    )
    parser.add_argument(
        "--strategy",
        choices=["single-centroid", "grid", "full-harvest"],
        default=None,
        help=(
            "Scrape strategy. 'single-centroid' (legacy depth loop), "
            "'grid' (== --grid), 'full-harvest' (grid + multi-query slow + "
            "fast ZIP top-up; empirically 39%% more coverage than grid alone). "
            "Defaults to 'grid' if --grid set, else 'single-centroid'."
        ),
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=None,
        help=(
            "Comma-separated query variants for the multi-query pass "
            "(full-harvest strategy only). Defaults to the 8-variant set matching "
"--query's vertical (plumbing or HVAC). Required when --query names "
"neither vertical, or names both — one vertical per run."
        ),
    )
    parser.add_argument(
        "--zip-csv",
        type=str,
        default=None,
        help=(
            "Path to CSV with 'zip,city,state' columns for the fast ZIP "
            "top-up pass (full-harvest strategy). Optional; ZIP pass is "
            "skipped if omitted."
        ),
    )
    parser.add_argument(
        "--pass2-combined",
        action="store_true",
        help=(
            "Opt into the legacy combined Pass 2 call (full-harvest only): "
            "one multi-query scrape covering all variants instead of a "
            "separate scrape per variant. Discouraged — the upstream "
            "scraper's shared deduper/exiter across a combined -input file "
            "silently drops most variants' results (see CLAUDE.md). Kept "
            "for comparison/diagnostic use; per-variant is the default."
        ),
    )
    parser.add_argument(
        "--scraper-concurrency",
        type=int,
        default=None,
        help="Override scraper -c concurrency for this run.",
    )
    parser.add_argument(
        "--scraper-browser-pool-size",
        type=int,
        default=None,
        help="Override scraper -browser-pool-size for this run.",
    )
    parser.add_argument(
        "--scraper-pages-per-browser",
        type=int,
        default=None,
        help="Override scraper -pages-per-browser for this run.",
    )
    parser.add_argument(
        "--scraper-proxy-limit",
        type=int,
        default=None,
        help="Limit forwarded scraper proxies to first N validated entries.",
    )
    parser.add_argument(
        "--scraper-disable-page-reuse",
        action="store_true",
        help="Pass upstream -disable-page-reuse for this run.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "After crawling, run Reacher (self-hosted check-if-email-exists) "
            "against every unverified contact email. Requires REACHER_API_URL "
            "to point at a live instance. Failures are logged but do not "
            "abort the pipeline."
        ),
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help=(
            "Only export contacts whose latest EmailVerification.score is >= N. "
            "0 (default) exports everything. Reacher scoring (see "
            "_SCORE_BY_STATUS in app/pipeline/verify_emails.py): safe=95, "
            "risky=50, unknown=25, invalid=10. Contacts with no verification "
            "row are treated as score=0 and skipped when min-score > 0."
        ),
    )
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help=(
            "Path for the local CSV export fallback (used only if Sheets "
            "export fails or SPREADSHEET_ID is unset/mock). Defaults to a "
            "descriptive 'data/leads_<location>_<query>_<date>.csv'."
        ),
    )
    return parser


def parse_args() -> argparse.Namespace:
    return _build_parser().parse_args()



def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"--bbox must be 'min_lat,min_lon,max_lat,max_lon' (4 floats), got {raw!r}"
        )
    try:
        min_lat, min_lon, max_lat, max_lon = (float(p) for p in parts)
    except ValueError as e:
        raise ValueError(f"--bbox values must be floats: {e}") from e
    if not (min_lat < max_lat and min_lon < max_lon):
        raise ValueError(
            f"--bbox must satisfy min_lat<max_lat and min_lon<max_lon, got {raw!r}"
        )
    return (min_lat, min_lon, max_lat, max_lon)


def _load_zip_csv(path: str) -> list[dict[str, str]]:
    """Load ZIP top-up rows: expect 'zip,city,state' columns."""
    import csv as _csv
    with open(path, "r", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for row in reader:
            z = (row.get("zip") or "").strip()
            city = (row.get("city") or "").strip()
            state = (row.get("state") or "").strip()
            if not z:
                continue
            rows.append({"zip": z, "city": city, "state": state})
    return rows


def _slugify(text: str) -> str:
    """First comma-separated segment, lowercased, alnum-only.

    e.g. 'San Jose, CA' -> 'sanjose'. Drops the state/qualifier so batch
    filenames stay short; callers that need the state should pre-join it.
    """
    primary = text.split(",")[0]
    return re.sub(r"[^a-z0-9]+", "", primary.lower()) or "na"


def _default_csv_path(query: str, location: str | None = None) -> str:
    """Descriptive default local-CSV export filename.

    e.g. 'data/leads_sanjose_plumbing_2026-07-29.csv'. `location` is
    omitted for batch runs spanning many ZIPs/cities under one query.
    """
    parts = ["leads"]
    if location:
        parts.append(_slugify(location))
    parts.append(_slugify(query))
    parts.append(date.today().isoformat())
    return f"data/{'_'.join(parts)}.csv"


def run_end_to_end_pipeline(
    query: str,
    location: str,
    min_contacts: int | None = None,
    max_depth: int | None = None,
    cell_km: float = 2.0,
    bbox: tuple[float, float, float, float] | None = None,
    disable_scraper_proxy: bool = False,
    disable_crawler_proxy: bool = False,
    strategy: str = "single-centroid",
    queries: tuple[str, ...] | None = None,
    zip_csv: str | None = None,
    verify: bool = False,
    min_score: int = 0,
    csv_path: str | None = None,
    scraper_concurrency: int | None = None,
    scraper_browser_pool_size: int | None = None,
    scraper_pages_per_browser: int | None = None,
    scraper_proxy_limit: int | None = None,
    scraper_disable_page_reuse: bool = False,
    pass2_per_variant: bool = True,
) -> None:
    """
    Orchestrate pipeline. Three strategies:

    - single-centroid (legacy): loop at increasing depths until
      min_contacts hit or max_depth reached.
    - grid: one scrape iterates cells over the location's bounding box
      (Nominatim-derived, or explicit `bbox` arg). No depth loop. Grid+d3
      is empirically 4-25x richer than a curated ZIP sweep.
    - full-harvest: grid + multi-query slow at centroid + optional fast
      ZIP top-up. Empirically 39% more coverage than grid alone
      (SJ 2026-07-20: grid=362, +multi-query=473, +fast-ZIP=504).

    `min_contacts` / `max_depth` gate the single-centroid depth loop only;
    grid and full-harvest ignore them, so non-single-centroid callers should
    pass None. None falls back to DEFAULT_MIN_CONTACTS / DEFAULT_MAX_DEPTH.

    `queries` overrides the full-harvest Pass 2 variant list. None (not an
    empty tuple) means "derive from the query's industry".

    `pass2_per_variant` (full-harvest only, default True): run each Pass 2
    variant as its own scrape, tagged separately in scrape_runs, instead of
    one combined multi-query call. See run_location_full_harvest docstring
    for why this is the default rather than a diagnostic opt-in.
    """
    setup_logging()

    logger.info("=" * 60)
    logger.info("STARTING END-TO-END LEAD GENERATION PIPELINE")
    logger.info(
        "query=%r location=%r min_contacts=%s strategy=%s",
        query,
        location,
        # None = not applicable to this strategy, not "zero".
        "n/a" if min_contacts is None else min_contacts,
        strategy,
    )
    logger.info("=" * 60)

    init_db()

    try:
        lat, lon, geo_bbox = geocode_location(location)
        if lat is None or lon is None:
            logger.warning("Could not geocode %r. Scraper will retry per iteration.", location)

        # Grid and full-harvest need a box; an explicit --bbox beats
        # Nominatim's. Both raise on None rather than degrading to a centroid
        # scrape — see run_location_grid.
        effective_bbox = bbox if bbox is not None else geo_bbox

        if strategy == "grid":
            run_location_grid(
                query=query,
                location=location,
                bbox=effective_bbox,
                cell_km=cell_km,
                disable_scraper_proxy=disable_scraper_proxy,
                disable_crawler_proxy=disable_crawler_proxy,
                scraper_concurrency=scraper_concurrency,
                scraper_browser_pool_size=scraper_browser_pool_size,
                scraper_pages_per_browser=scraper_pages_per_browser,
                scraper_proxy_limit=scraper_proxy_limit,
                scraper_disable_page_reuse=scraper_disable_page_reuse,
            )
        elif strategy == "full-harvest":
            run_location_full_harvest(
                query=query,
                location=location,
                bbox=effective_bbox,
                # Centroid already resolved above — don't geocode twice.
                lat=lat,
                lon=lon,
                cell_km=cell_km,
                queries=queries,
                zip_csv=zip_csv,
                disable_scraper_proxy=disable_scraper_proxy,
                disable_crawler_proxy=disable_crawler_proxy,
                scraper_concurrency=scraper_concurrency,
                scraper_browser_pool_size=scraper_browser_pool_size,
                scraper_pages_per_browser=scraper_pages_per_browser,
                scraper_proxy_limit=scraper_proxy_limit,
                scraper_disable_page_reuse=scraper_disable_page_reuse,
                pass2_per_variant=pass2_per_variant,
            )
        else:
            # single-centroid legacy — the only strategy that reads
            # min_contacts / max_depth. Delegates to run_location_pipeline so
            # there is one depth-loop implementation, shared with
            # run_zip_batch.py, and one definition of "enough contacts".
            contact_target = (
                DEFAULT_MIN_CONTACTS if min_contacts is None else min_contacts
            )
            depth_limit = DEFAULT_MAX_DEPTH if max_depth is None else max_depth
            metrics = run_location_pipeline(
                query=query,
                location=location,
                max_depth=depth_limit,
                target_new_exportable=contact_target,
                disable_scraper_proxy=disable_scraper_proxy,
                disable_crawler_proxy=disable_crawler_proxy,
                scraper_concurrency=scraper_concurrency,
                scraper_browser_pool_size=scraper_browser_pool_size,
                scraper_pages_per_browser=scraper_pages_per_browser,
                scraper_proxy_limit=scraper_proxy_limit,
                scraper_disable_page_reuse=scraper_disable_page_reuse,
                # Centroid already resolved above — don't geocode twice.
                lat=lat,
                lon=lon,
            )
            logger.info(
                "Single-centroid complete. Depths run: %s. New exportable "
                "contacts: %d (target %d). Contacts in DB: %d.",
                ", ".join(str(d) for d in metrics.depths_run),
                metrics.new_exportable_contacts,
                contact_target,
                metrics.total_contacts,
            )

        if verify:
            logger.info("--- Verifying harvested emails via Reacher ---")
            try:
                verify_contacts_emails()
            except Exception as ve:  # noqa: BLE001
                # Verifier is best-effort — server can be down / port 25
                # blocked. Log and keep going so we still get an export.
                logger.warning("Verification pass failed: %s", ve)

        export_run_outputs(
            min_score=min_score,
            csv_path=csv_path or _default_csv_path(query, location),
        )
        logger.info("=" * 60)
        logger.info("PIPELINE EXECUTED SUCCESSFULLY")
        logger.info("=" * 60)

    except Exception as e:
        logger.exception("Pipeline run aborted: %s", e)
        sys.exit(1)



def _resolve_strategy(args: argparse.Namespace) -> str:
    """Resolve strategy from CLI flags. Explicit --strategy wins."""
    if args.strategy:
        return args.strategy
    if args.grid:
        return "grid"
    return "single-centroid"


def _validate_positive_counts(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    """Reject non-positive values for the count/depth flags (review #16).

    Both default to None ("flag not passed"), so only an explicitly supplied
    value is checked. A `--min-contacts 0` target is met before the first
    scrape; a `--max-depth 0` loop can't run an iteration at all.
    """
    for flag, value in (
        ("--min-contacts", args.min_contacts),
        ("--max-depth", args.max_depth),
    ):
        if value is not None and value <= 0:
            parser.error(f"{flag} must be > 0 (got {value}).")


def _resolve_query_variants(
    args: argparse.Namespace,
    strategy: str,
    parser: argparse.ArgumentParser,
) -> tuple[str, ...] | None:
    """Validate flag/strategy combinations and resolve --queries.

    Flags that change *what gets scraped* hard-error when the selected
    strategy would drop them — a warning that scrolls past in cron output is
    how users end up trusting a harvest that never ran what they asked for.
    Flags that only cap a loop (--min-contacts/--max-depth) stay warnings.
    """
    if args.queries and strategy != "full-harvest":
        parser.error(
            f"--queries only applies to --strategy full-harvest (got "
            f"{strategy}); drop the flag or switch strategy."
        )

    query_variants: tuple[str, ...] | None = None
    if args.queries:
        query_variants = tuple(q.strip() for q in args.queries.split(",") if q.strip())
        if not query_variants:
            parser.error("--queries contained no non-empty query variants.")

    # Full-harvest's coverage edge over grid is Pass 2's multi-query sweep. If
    # we can't derive a variant set and the user didn't supply one, that pass
    # degenerates to the base query — fail now rather than burn full-harvest
    # wall time for grid-level results.
    if query_variants is None and strategy == "full-harvest":
        if _default_harvest_queries(args.query) is None:
            parser.error(
                f"no built-in harvest query set for --query {args.query!r} "
                f"(unrecognized vertical, or it names both plumbing and HVAC "
                f"— run one vertical per run). full-harvest PASS 2 would run "
                f"the base query alone and yield roughly grid-only coverage. "
                f'Pass --queries "variant1,variant2,..." to define the sweep.'
            )

    return query_variants


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _validate_positive_counts(args, parser)
    bbox = _parse_bbox(args.bbox) if args.bbox else None
    strategy = _resolve_strategy(args)

    if bbox is not None and strategy not in ("grid", "full-harvest"):
        logger.warning("--bbox supplied but strategy is %s; bbox will be ignored.", strategy)
    if args.zip_csv and strategy != "full-harvest":
        logger.warning("--zip-csv supplied but strategy is %s; zip-csv ignored.", strategy)
    if args.pass2_combined and strategy != "full-harvest":
        logger.warning(
            "--pass2-combined supplied but strategy is %s; ignored (only "
            "full-harvest has a Pass 2).",
            strategy,
        )
    # --min-contacts / --max-depth only gate the single-centroid depth loop.
    # Both default to None, so a non-None value means the user really passed
    # the flag — warn so they don't think they're bounding grid/full-harvest.
    if strategy != "single-centroid":
        for flag, value in (
            ("--min-contacts", args.min_contacts),
            ("--max-depth", args.max_depth),
        ):
            if value is not None:
                logger.warning(
                    "%s=%d supplied but strategy is %s; ignored (only "
                    "single-centroid gates on %s).",
                    flag,
                    value,
                    strategy,
                    flag,
                )

    query_variants = _resolve_query_variants(args, strategy, parser)

    disable_scraper_proxy = args.no_proxy or args.no_scraper_proxy
    disable_crawler_proxy = args.no_proxy or args.no_crawler_proxy

    run_end_to_end_pipeline(
        query=args.query,
        location=args.location,
        # Pass None for strategies that ignore these, so the signature
        # reflects which knobs are actually live for this run.
        min_contacts=args.min_contacts if strategy == "single-centroid" else None,
        max_depth=args.max_depth if strategy == "single-centroid" else None,
        cell_km=args.cell_km,
        bbox=bbox,
        disable_scraper_proxy=disable_scraper_proxy,
        disable_crawler_proxy=disable_crawler_proxy,
        strategy=strategy,
        queries=query_variants,
        zip_csv=args.zip_csv,
        verify=args.verify,
        min_score=args.min_score,
        csv_path=args.csv_path,
        scraper_concurrency=args.scraper_concurrency,
        scraper_browser_pool_size=args.scraper_browser_pool_size,
        scraper_pages_per_browser=args.scraper_pages_per_browser,
        scraper_proxy_limit=args.scraper_proxy_limit,
        scraper_disable_page_reuse=args.scraper_disable_page_reuse,
        # Per-variant is the default; --pass2-combined opts back into the
        # legacy combined call. Irrelevant (Pass 2 doesn't run) for other
        # strategies, so it always resolves to the default there.
        pass2_per_variant=not (strategy == "full-harvest" and args.pass2_combined),
    )


if __name__ == "__main__":
    main()
