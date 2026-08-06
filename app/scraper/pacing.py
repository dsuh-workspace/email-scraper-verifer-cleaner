"""Jittered pacing between scraper invocations.

Per-action delays (between clicks, scrolls, or detail visits) are not reachable
from here: the scraper is a Go subprocess and exposes no jitter knobs. What is
reachable is the gap *between* invocations, and the pipeline already runs many
of them — one per full-harvest Pass 2 variant, one per Pass 3 ZIP, one per
`run_zip_batch.py` row, one per single-centroid depth step.

Off unless `SCRAPER_PACING_SEC` is set, so run wall-clock in `RUNS.md` stays
comparable until an operator opts in. Format is `MIN:MAX` seconds (e.g.
`10:20`); a bare number means a fixed delay.
"""

import logging
import os
import random
import time

logger = logging.getLogger(__name__)

ENV_VAR = "SCRAPER_PACING_SEC"


def pacing_range() -> tuple[float, float] | None:
    """Parse `SCRAPER_PACING_SEC`. Returns None when pacing is off.

    Invalid values warn and disable pacing rather than raising — pacing is a
    politeness knob and must never be the reason a run dies.
    """
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        return None

    parts = raw.split(":")
    if len(parts) > 2:
        logger.warning("%s=%r is not MIN:MAX — pacing disabled.", ENV_VAR, raw)
        return None

    try:
        values = [float(part.strip()) for part in parts]
    except ValueError:
        logger.warning("%s=%r is not numeric — pacing disabled.", ENV_VAR, raw)
        return None

    low, high = (values[0], values[0]) if len(values) == 1 else (values[0], values[1])
    if low < 0 or high < 0:
        logger.warning("%s=%r must be non-negative — pacing disabled.", ENV_VAR, raw)
        return None
    if high < low:
        logger.warning("%s=%r has MAX < MIN — pacing disabled.", ENV_VAR, raw)
        return None
    if high == 0:
        return None

    return low, high


def pace(reason: str) -> float:
    """Sleep a jittered interval. Returns seconds slept (0.0 when off)."""
    window = pacing_range()
    if window is None:
        return 0.0

    low, high = window
    delay = random.uniform(low, high)
    logger.info("Pacing %.1fs before %s.", delay, reason)
    time.sleep(delay)
    return delay
