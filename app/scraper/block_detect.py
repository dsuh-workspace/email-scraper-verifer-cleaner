"""Detect scrape runs that look soft-blocked rather than genuinely thin.

Neither this wrapper nor upstream `gosom/google-maps-scraper` inspects pages
for Google's `/sorry/` interstitial or a captcha — grep the Go tree and there
is no such check. So a soft-blocked run is indistinguishable from a thin
market: it returns few or zero leads and `run_scraper.py` marks it
`completed`.

Blocking cannot be observed directly from here (the scraper is a subprocess
that only hands back JSON), so this module works on the one signal that does
survive: yield. Two rules, deliberately conservative because a false positive
parks a working proxy —

1. Zero leads from a proxied run. A Maps search essentially always returns
   something nearby; zero means blocked, or the scraper failed quietly.
2. Yield far below what this same query + location produced before, once
   enough history exists to have a median worth comparing against.

Rule 2's history is scoped to query + location, which is the tightest
comparison available. It is still noisy: full-harvest runs the same
query/location at different depths and modes (grid pass, slow centroid pass,
fast ZIP top-up), and those legitimately yield very different counts. Hence
the low default ratio — this is a flag for the operator, not a gate.
"""

import logging
import os
from statistics import median

from sqlalchemy import func

from app.db.create_tables import RawLead, ScrapeRun

logger = logging.getLogger(__name__)

STATUS_COMPLETED = "completed"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
# Killed at SCRAPER_TIMEOUT_SEC with partial output salvaged. Distinct from
# `failed` so a wall-clock truncation isn't read as a crash, and — like
# `blocked` — excluded from `recent_yields` below, so a truncated sweep never
# becomes the baseline later runs are judged against.
STATUS_TIMEOUT = "timeout"

# Need at least this many prior runs before a median means anything.
DEFAULT_MIN_HISTORY = 3
# Flag when yield drops below this fraction of the historical median.
DEFAULT_LOW_YIELD_RATIO = 0.25
DEFAULT_HISTORY_LIMIT = 10

_FALSEY = {"0", "false", "no", "off"}


def detection_enabled() -> bool:
    return os.getenv("BLOCK_DETECT_ENABLED", "1").strip().lower() not in _FALSEY


def zero_yield_enabled() -> bool:
    return os.getenv("BLOCK_DETECT_ZERO_YIELD", "1").strip().lower() not in _FALSEY


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s must be a number, got %r — using %s.", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s must be > 0, got %s — using %s.", name, value, default)
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s must be an integer, got %r — using %d.", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s must be > 0, got %d — using %d.", name, value, default)
        return default
    return value


def classify_yield(
    lead_count: int,
    history: list[int],
    *,
    min_history: int | None = None,
    low_yield_ratio: float | None = None,
    allow_zero_yield_rule: bool | None = None,
) -> str | None:
    """Return a block reason for this yield, or None if it looks normal.

    Pure function — no DB, no env reads unless the caller leaves an argument
    None. `history` is prior lead counts for the same query + location, most
    recent first.
    """
    if min_history is None:
        min_history = _env_int("BLOCK_DETECT_MIN_HISTORY", DEFAULT_MIN_HISTORY)
    if low_yield_ratio is None:
        low_yield_ratio = _env_float("BLOCK_DETECT_LOW_YIELD_RATIO", DEFAULT_LOW_YIELD_RATIO)
    if allow_zero_yield_rule is None:
        allow_zero_yield_rule = zero_yield_enabled()

    if lead_count <= 0:
        return "zero-yield" if allow_zero_yield_rule else None

    usable_history = [count for count in history if count is not None]
    if len(usable_history) < min_history:
        return None

    baseline = median(usable_history)
    if baseline <= 0:
        return None

    if lead_count < low_yield_ratio * baseline:
        return f"low-yield ({lead_count} vs median {baseline:g})"

    return None


def recent_yields(session, *, query: str, location: str, limit: int | None = None) -> list[int]:
    """Prior lead counts for this query + location, most recent run first.

    Only `completed` runs count: `failed` runs never ingested, and `blocked`
    runs are exactly the depressed numbers we must not fold into the baseline.
    """
    if limit is None:
        limit = _env_int("BLOCK_DETECT_HISTORY_LIMIT", DEFAULT_HISTORY_LIMIT)

    rows = (
        session.query(func.count(RawLead.id))
        .select_from(ScrapeRun)
        .outerjoin(RawLead, RawLead.scrape_run_id == ScrapeRun.id)
        .filter(
            ScrapeRun.query == query,
            ScrapeRun.location == location,
            ScrapeRun.status == STATUS_COMPLETED,
        )
        .group_by(ScrapeRun.id)
        .order_by(ScrapeRun.id.desc())
        .limit(limit)
        .all()
    )
    return [int(row[0] or 0) for row in rows]
