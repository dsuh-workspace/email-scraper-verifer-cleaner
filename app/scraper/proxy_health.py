"""Proxy health ledger: strikes, cooldowns, and retirement.

Neither this wrapper nor upstream `gosom/google-maps-scraper` has any block
detection — a soft-blocked run comes back with few or zero leads and is
recorded as a normal completion. This module is the cooldown half of the fix:
`block_detect.py` decides a run looks blocked, and this module remembers which
proxies were in play so the next run stops using them for a while.

State lives in a small JSON file (`PROXY_HEALTH_FILE`, default
`data/proxy_health.json`) rather than the DB: it is mutable local operating
state, an operator may want to reset it by hand, and the pipeline already
assumes one process per DB.

Passwords are never written to the file — proxies are keyed by
`user@host:port`.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_PROXY_HEALTH_FILE = os.path.join("data", "proxy_health.json")

# How long a run may wait for a cooling proxy before giving up. Covers one
# default cooldown with room to spare; a retirement-length park exceeds it, so
# a genuinely dead pool still fails loudly instead of hanging for a day.
DEFAULT_WAIT_MAX_SEC = 900

# First strike parks a proxy for this long; `PROXY_RETIRE_AFTER_STRIKES`
# strikes park it for `PROXY_RETIRE_SEC` (effectively "retired for the run").
DEFAULT_COOLDOWN_SEC = 600
DEFAULT_RETIRE_AFTER_STRIKES = 2
DEFAULT_RETIRE_SEC = 86400


class ProxyPoolExhausted(RuntimeError):
    """Every configured proxy is cooling down.

    Raised rather than returning an empty proxy list: an empty list makes the
    scraper hit Google directly from the host IP, which is worse than a loud
    failure. `run_zip_batch.py` catches per-row exceptions, so a batch logs
    this and moves on instead of silently de-proxying.
    """


def proxy_id(proxy_url: str) -> str:
    """Stable, password-free identity for a proxy URL."""
    parsed = urlparse(proxy_url)
    host = parsed.hostname or "?"
    port = parsed.port or "?"
    if parsed.username:
        return f"{parsed.username}@{host}:{port}"
    return f"{host}:{port}"


def _env_int(name: str, default: int, *, allow_zero: bool = False) -> int:
    """Positive-int env read. `allow_zero` permits 0 as an explicit "off"."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s must be an integer, got %r — using %d.", name, raw, default)
        return default
    floor = 0 if allow_zero else 1
    if value < floor:
        logger.warning("%s must be >= %d, got %d — using %d.", name, floor, value, default)
        return default
    return value


def state_path() -> str:
    return os.getenv("PROXY_HEALTH_FILE", "").strip() or DEFAULT_PROXY_HEALTH_FILE


def load_state() -> dict:
    path = state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as e:
        # Corrupt ledger must not stop a scrape; worst case we forget cooldowns.
        logger.warning("Could not read proxy health file %s (%s). Starting empty.", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("Proxy health file %s is not an object. Starting empty.", path)
        return {}
    return data


def save_state(state: dict) -> None:
    path = state_path()
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning("Could not write proxy health file %s (%s).", path, e)


def _cooldown_remaining(entry: dict, now: datetime) -> timedelta | None:
    raw = (entry or {}).get("cooldown_until")
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    remaining = until - now
    return remaining if remaining.total_seconds() > 0 else None


def filter_cooling(proxies: list[str]) -> tuple[list[str], dict[str, timedelta]]:
    """Split `proxies` into (usable, {proxy_id: time_remaining}).

    Order of the usable list is preserved so sticky assignment stays stable
    while a proxy is parked.
    """
    if not proxies:
        return [], {}

    state = load_state()
    now = datetime.now(timezone.utc)
    usable: list[str] = []
    cooling: dict[str, timedelta] = {}

    for proxy_url in proxies:
        pid = proxy_id(proxy_url)
        remaining = _cooldown_remaining(state.get(pid, {}), now)
        if remaining is None:
            usable.append(proxy_url)
        else:
            cooling[pid] = remaining

    return usable, cooling


def earliest_expiry(proxies: list[str]) -> timedelta | None:
    """Shortest remaining cooldown across `proxies`, or None if any is usable."""
    _, cooling = filter_cooling(proxies)
    if not cooling or len(cooling) < len(proxies):
        return None
    return min(cooling.values())


def wait_for_capacity(proxies: list[str]) -> float:
    """Block until a proxy's cooldown expires. Returns seconds slept.

    A small pool plus a full-width proxy limit means one flagged run can park
    every proxy at once — and a batch that treats that as a hard error would
    burn through its remaining rows. Waiting out the shortest cooldown matches
    the intended policy (park briefly, then resume) instead.

    Returns 0.0 when nothing needs waiting or when the required wait exceeds
    `PROXY_WAIT_MAX_SEC`; the caller decides whether that is fatal.
    """
    remaining = earliest_expiry(proxies)
    if remaining is None:
        return 0.0

    max_wait = _env_int("PROXY_WAIT_MAX_SEC", DEFAULT_WAIT_MAX_SEC, allow_zero=True)
    if max_wait <= 0:
        return 0.0

    seconds = remaining.total_seconds()
    if seconds > max_wait:
        logger.warning(
            "All proxies cooling for another %.0fs, which exceeds "
            "PROXY_WAIT_MAX_SEC=%d — not waiting.", seconds, max_wait,
        )
        return 0.0

    # +1s so the cooldown is definitely past when we re-check.
    seconds += 1
    logger.warning("All proxies cooling; waiting %.0fs for the first to free up.", seconds)
    time.sleep(seconds)
    return seconds


def record_block(proxies: list[str], reason: str) -> None:
    """Add a strike to every proxy used by a run that looked blocked."""
    if not proxies:
        return

    cooldown_sec = _env_int("PROXY_COOLDOWN_SEC", DEFAULT_COOLDOWN_SEC)
    retire_after = _env_int("PROXY_RETIRE_AFTER_STRIKES", DEFAULT_RETIRE_AFTER_STRIKES)
    retire_sec = _env_int("PROXY_RETIRE_SEC", DEFAULT_RETIRE_SEC)

    state = load_state()
    now = datetime.now(timezone.utc)

    for proxy_url in proxies:
        pid = proxy_id(proxy_url)
        entry = state.get(pid) or {}
        strikes = int(entry.get("strikes") or 0) + 1
        park_sec = retire_sec if strikes >= retire_after else cooldown_sec
        entry.update(
            strikes=strikes,
            reason=reason,
            last_block_at=now.isoformat(),
            cooldown_until=(now + timedelta(seconds=park_sec)).isoformat(),
        )
        state[pid] = entry
        logger.warning(
            "Proxy %s parked for %ds (strike %d/%d, reason=%s).",
            pid, park_sec, strikes, retire_after, reason,
        )

    save_state(state)


def record_success(proxies: list[str]) -> None:
    """Decay one strike for proxies that just produced a healthy run.

    Decay rather than reset: without any forgiveness, strikes accumulate
    across unrelated thin markets until a good proxy retires itself; with a
    full reset, a proxy that alternates block/success never reaches the retire
    threshold at all.
    """
    if not proxies:
        return

    state = load_state()
    changed = False
    now = datetime.now(timezone.utc).isoformat()

    for proxy_url in proxies:
        pid = proxy_id(proxy_url)
        entry = state.get(pid)
        if not entry:
            continue
        strikes = int(entry.get("strikes") or 0)
        if strikes <= 0 and not entry.get("cooldown_until"):
            continue
        entry["strikes"] = max(0, strikes - 1)
        entry["last_success_at"] = now
        entry.pop("cooldown_until", None)
        state[pid] = entry
        changed = True

    if changed:
        save_state(state)
