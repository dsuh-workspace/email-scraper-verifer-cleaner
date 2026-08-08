import hashlib
import logging
import os
import json
import platform
import shutil
import signal
import subprocess
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone

from geopy.geocoders import Nominatim
from sqlalchemy.orm import sessionmaker

from app.db.database import engine
from app.db.create_tables import ScrapeRun, RawLead
from app.logging_config import setup_logging
from app.proxy_utils import load_proxy_file, normalize_proxy_line, validate_proxy_url
from app.scraper.block_detect import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    classify_yield,
    detection_enabled,
    recent_yields,
)
from app.scraper.proxy_health import (
    ProxyPoolExhausted,
    filter_cooling,
    record_block,
    record_success,
    state_path,
    wait_for_capacity,
)

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


def _session_offset(session_key: str | None, count: int) -> int:
    """Stable rotation offset for a sticky proxy session.

    Uses a content hash, not `hash()`: str hashing is salted per process, so
    `hash()` would hand the same variant a different proxy on every
    invocation — the exact behaviour this replaces.
    """
    if not session_key or count <= 1:
        return 0
    digest = hashlib.blake2b(session_key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % count


def _select_scraper_proxies(
    disable_proxy: bool = False,
    proxy_limit: int | None = None,
    session_key: str | None = None,
) -> list[str]:
    """Resolve the proxy subset for one invocation.

    Assignment is *sticky*: the same `session_key` (the query variant) always
    lands on the same slice of the pool. This replaced a `random.shuffle`,
    which redrew a random subset on every invocation — so a variant could not
    hold one proxy across its run, and a proxy that got a run blocked was
    unattributable and reshuffled straight back into the next one.

    Proxies cooling down from an earlier suspected block are dropped first;
    the rotation is computed over what is left.
    """
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

    usable, cooling = filter_cooling(proxies)
    if cooling:
        logger.warning(
            "Skipping %d cooling proxy/proxies: %s.",
            len(cooling),
            ", ".join(
                f"{pid} ({int(remaining.total_seconds() // 60)}m left)"
                for pid, remaining in sorted(cooling.items())
            ),
        )
    if not usable:
        # Wait out the shortest cooldown before giving up: with a small pool
        # one flagged run can park everything, and failing here would take the
        # rest of a batch down with it.
        if wait_for_capacity(proxies):
            usable, _ = filter_cooling(proxies)

    if not usable:
        raise ProxyPoolExhausted(
            f"All {len(proxies)} configured proxies are cooling down. "
            "Wait for a cooldown to expire, add proxies, or clear "
            f"{state_path()} if the strikes were false positives. "
            "Pass --no-scraper-proxy only if scraping unproxied is acceptable."
        )

    offset = _session_offset(session_key, len(usable))
    rotated = usable[offset:] + usable[:offset]

    if limit is not None:
        rotated = rotated[:limit]

    logger.info(
        "Scraper proxies enabled (%d of %d usable%s).",
        len(rotated),
        len(usable),
        f", sticky session {session_key!r}" if session_key else "",
    )
    return rotated


def _assess_run_health(
    session,
    *,
    query: str,
    location: str,
    ingested_count: int,
    session_proxies: list[str],
) -> str | None:
    """Flag a suspected block and update the proxy ledger. Returns a reason.

    Never raises: this is diagnostics layered onto a run that already
    succeeded, so a broken history query must not lose the ingested leads.
    """
    if not detection_enabled():
        return None

    # With no proxies there is nothing to charge a block to, and a direct-IP
    # run yielding zero is a different problem.
    if not session_proxies:
        return None

    try:
        history = recent_yields(session, query=query, location=location)
        reason = classify_yield(ingested_count, history)
        if reason:
            record_block(session_proxies, reason)
        else:
            record_success(session_proxies)
        return reason
    except Exception as e:
        logger.warning("Block detection skipped (%s).", e)
        return None


def _format_proxy_cmd_args(proxies: list[str]) -> list[str]:
    """Turn an already-selected proxy list into the upstream gosom `-proxies`
    flag. The one place that formats proxies for the scraper CLI, shared by
    `_scraper_proxy_args` (tests, `smoke_test_scraper_proxies.py`) and
    `execute_scrape_and_ingest` (production) so they can't drift apart."""
    if not proxies:
        return []
    return ["-proxies", ",".join(proxies)]


def _scraper_proxy_args(
    disable_proxy: bool = False,
    proxy_limit: int | None = None,
    session_key: str | None = None,
) -> list[str]:
    """Upstream gosom proxy args for the selected subset."""
    proxies = _select_scraper_proxies(
        disable_proxy=disable_proxy,
        proxy_limit=proxy_limit,
        session_key=session_key,
    )
    return _format_proxy_cmd_args(proxies)


def _preserve_results_file(results_file_path: str, scrape_run_id: int) -> str | None:
    """Copy a killed run's partial output somewhere durable. Returns the path.

    The `-results` target is a tempfile that the `finally` block removes, and
    `subprocess.run(timeout=...)` throws away stdout/stderr, so without this
    a timeout leaves no evidence at all of how far the sweep got. Best-effort
    by design: failing to save a diagnostic must not mask the timeout itself.
    """
    try:
        if not os.path.exists(results_file_path) or os.path.getsize(results_file_path) == 0:
            logger.warning("Nothing to preserve — results file is empty or missing.")
            return None
        os.makedirs("logs", exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = os.path.join("logs", f"timeout_run{scrape_run_id}_{stamp}.json")
        shutil.copyfile(results_file_path, dest)
        return dest
    except OSError as e:
        logger.warning("Could not preserve partial results file: %s", e)
        return None


def _preserve_scraper_log(log_path: str, scrape_run_id: int) -> str | None:
    """Copy a killed run's streamed stdout/stderr log somewhere durable.

    `_preserve_results_file` recovers *what* got scraped from `-results`,
    but that file only ever holds finished leads — it says nothing about
    where in a grid sweep the scraper was when it got killed. This log is
    written line-by-line as the scraper runs (see `_PipeStreamer`), so its
    tail is the closest thing to a position marker we have. CLAUDE.md Open
    work #4. Best-effort by design, same as its results-file sibling.
    """
    try:
        if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
            logger.warning("Nothing to preserve — scraper log is empty or missing.")
            return None
        os.makedirs("logs", exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = os.path.join("logs", f"timeout_run{scrape_run_id}_{stamp}.log")
        shutil.copyfile(log_path, dest)
        return dest
    except OSError as e:
        logger.warning("Could not preserve scraper log: %s", e)
        return None


class _PipeStreamer:
    """Drains a subprocess pipe in a background thread instead of buffering
    it all in memory for a final `communicate()`.

    Two things this earns us:

    - `proc.wait(timeout=)` can be used instead of `proc.communicate(timeout=)`
      without risking the classic deadlock (child blocks writing once its
      stdout/stderr pipe fills, parent blocks waiting for exit) — the
      threads keep the pipes drained the whole time.
    - Every line the scraper prints lands on disk as it's produced, so a
      killed run's log always has its most recent lines. `-results` only
      ever holds *finished* leads and says nothing about which grid cell
      the scraper was on when it died; the stdout/stderr tail is what
      actually shows position. CLAUDE.md Open work #4.

    Only the last `tail_lines` are kept in memory (for `CalledProcessError`
    messages); the full stream goes to `log_handle`.
    """

    def __init__(self, pipe, log_handle, label: str, tail_lines: int = 200):
        self._pipe = pipe
        self._log_handle = log_handle
        self._label = label
        self._tail: deque[str] = deque(maxlen=tail_lines)
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "_PipeStreamer":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            for line in iter(self._pipe.readline, ""):
                if not line:
                    break
                with self._lock:
                    self._tail.append(line)
                try:
                    self._log_handle.write(f"[{self._label}] {line}")
                    self._log_handle.flush()
                except (OSError, ValueError):
                    # Handle closed out from under us — nothing to do.
                    pass
        finally:
            try:
                self._pipe.close()
            except OSError:
                pass

    def join(self, timeout: float = 5) -> None:
        self._thread.join(timeout)

    def tail_text(self) -> str:
        with self._lock:
            return "".join(self._tail)


def _warn_stale_scraper_processes(binary_path: str) -> None:
    """Best-effort startup check for scraper processes left running by a
    parent that died without cleanup (Ctrl-C, SIGKILL, crashed shell — see
    CLAUDE.md Open work #3). Logs a warning only; never kills automatically,
    since a hit could just as easily be a concurrent pipeline run and
    killing on a guess would be worse than a stale bandwidth reading.
    """
    if platform.system() == "Windows":
        return
    binary_name = os.path.basename(binary_path)
    try:
        result = subprocess.run(
            ["pgrep", "-f", binary_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            timeout=5,
        )
        pids = [p for p in result.stdout.split() if p]
    except Exception as e:
        # Genuinely best-effort: no pgrep, a sandboxed test double for the
        # scraper's own Popen call intercepting this one too, anything. A
        # diagnostic must never break the real scrape it's checking before.
        logger.debug("Stale-process check skipped: %s", e)
        return
    if pids:
        logger.warning(
            "%d pre-existing %s process(es) found before starting this run "
            "(PIDs: %s). Could be orphans from a killed pipeline still "
            "burning proxy bandwidth (CLAUDE.md Open work #3) — verify with "
            "`ps -p <pid>` before trusting any bandwidth measurement.",
            len(pids), binary_name, ", ".join(pids),
        )


def _popen_group_kwargs() -> dict:
    """Process-group kwargs so killing the scraper takes its Playwright /
    Chromium grandchildren down with it, not just the Go binary's own pid.
    POSIX: `start_new_session` makes the scraper a session leader, so
    `os.killpg` reaches every descendant. Windows: `CREATE_NEW_PROCESS_GROUP`
    is the closest equivalent, paired with CTRL_BREAK_EVENT below.
    """
    if platform.system() == "Windows":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_scraper_process_group(proc: "subprocess.Popen") -> None:
    """Kill the scraper's whole process group/tree, not just its direct pid.

    Playwright spawns Chromium as a grandchild of the Go binary, so killing
    only `proc.pid` (what `subprocess.run(timeout=)` / a bare `.kill()` do)
    leaves the browser — and its bandwidth use — running after the parent
    is gone. This is the fix for CLAUDE.md Open work #3. Best-effort: the
    group may already be gone by the time we get here.
    """
    try:
        if platform.system() == "Windows":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError) as e:
        logger.debug("Scraper process group already gone: %s", e)


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
    proxy_session_key: str | None = None,
    extract_email: bool | None = None,
    timeout_sec: int | None = None,
):
    """
    Runs the google-maps-scraper executable for a query, then parses the
    resulting JSON and ingests it into the SQLite database.

    Three modes:
    - Single-centroid (default): pass lat/lon (or let it geocode from
      location), scraper uses -geo and -fast-mode. ~10-20 businesses per run.
    - Grid mode: pass bbox=(min_lat, min_lon, max_lat, max_lon) and cell_km.
      Scraper iterates cells internally in JS mode. -fast-mode is dropped;
      scraper rejects it with -grid-bbox. The "4-5x more businesses than a
      curated ZIP sweep" figure is from the 2026-07-20 experiment, which
      ran unproxied over a tight hand-picked bbox at 3km cells. Note that
      JS mode binds one proxy per browser context and the pool defaults to
      one, so a proxied grid pass sends every cell through a single IP —
      see README, "Grid mode and proxy binding".
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

    extract_email override (upstream `-email`):
      - None (default) => False for grid (bbox set), True otherwise.
      - True/False => explicit.
      Grid defaults off because upstream spawns one extra browser visit per
      place with a website and withholds the place entry until that visit
      returns, which multiplies across cells. The pipeline's own crawl
      (`app/pipeline/extract_emails.py`) covers the same ground afterwards
      with concurrency, per-host politeness, a retry ledger, and the shared
      junk filter — none of which the scraper's inline pass has. With this
      off, `raw_leads.email` is empty and emails arrive via that crawl.

    On timeout the partial `-results` file is ingested rather than
    discarded, and a copy is kept under `logs/`; the run is recorded as
    `timeout` and the TimeoutExpired still propagates.
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
    # Set by the timeout branch so the tail knows to record a salvage rather
    # than a clean completion, and so the outer handler doesn't relabel it.
    timed_out: subprocess.TimeoutExpired | None = None

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

    # 2. Set up temporary files for query, results, and the streamed
    # stdout/stderr log (see _PipeStreamer). Use temporary files so we
    # don't pollute the workspace.
    fd_query, query_file_path = tempfile.mkstemp(suffix=".txt", prefix="query_")
    fd_results, results_file_path = tempfile.mkstemp(suffix=".json", prefix="results_")
    fd_scraper_log, scraper_log_path = tempfile.mkstemp(suffix=".log", prefix="scraperlog_")
    os.close(fd_scraper_log)

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

        _warn_stale_scraper_processes(binary_path)

        # Resolve fast_mode: explicit param wins; else default True unless grid.
        use_fast_mode = fast_mode if fast_mode is not None else (bbox is None)
        if use_fast_mode and bbox is not None:
            raise ValueError(
                "fast_mode=True is incompatible with grid mode (bbox). "
                "Scraper rejects the combination."
            )

        # Inline email extraction: upstream spawns a *separate browser visit
        # to each business's own website* for every place result whose website
        # passes IsWebsiteValidForEmail (gmaps/place.go:132), and — critically
        # — returns nil for the place entry, so nothing is emitted until that
        # visit finishes. 93.8% of observed raw leads have a website, so this
        # roughly doubles the browser work per result and gates all output
        # behind it. Fine at one centroid; ruinous across a few hundred grid
        # cells, where it turned a San Jose sweep into a 1800s timeout with
        # zero rows written. Default off for grid, on elsewhere.
        use_extract_email = (
            extract_email if extract_email is not None else (bbox is None)
        )

        cmd = [
            binary_path,
            "-input", query_file_path,
            "-results", results_file_path,
            "-json",
            "-depth", str(depth),
            "-pages-per-browser", str(resolved_pages_per_browser),
            "-lang", lang,
        ]
        if use_extract_email:
            cmd.append("-email")
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
        # Selected once and kept, so a suspected block can be charged back to
        # the exact proxies that were in play.
        session_proxies = _select_scraper_proxies(
            disable_proxy=disable_proxy,
            proxy_limit=proxy_limit,
            session_key=proxy_session_key or query,
        )
        cmd.extend(_format_proxy_cmd_args(session_proxies))

        logger.info("Executing: %s", " ".join(cmd))
        # Run the scraper.
        # Redirect stdout/stderr to capture runtime diagnostics.
        # Timeout is a hard 30-min ceiling — deeper crawls (depth>10) can
        # legitimately take a while, but we never want a hung Playwright
        # instance to freeze the pipeline forever. Override via
        # SCRAPER_TIMEOUT_SEC env var if needed.
        # Explicit caller value wins, then the env override, then 30 min.
        # Grid callers derive theirs from cell count (run_pipeline's
        # `_grid_preflight`), because a fixed ceiling is meaningless when the
        # sweep size varies by two orders of magnitude between cities.
        scraper_timeout = timeout_sec or int(os.getenv("SCRAPER_TIMEOUT_SEC", "1800"))
        # Popen (not subprocess.run) so we hold the pid and can kill the
        # whole process group. subprocess.run's own timeout/exception
        # handling only kills the direct child pid, leaving Playwright's
        # Chromium grandchildren running as orphans — CLAUDE.md Open work #3.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            **_popen_group_kwargs(),
        )
        # Drain stdout/stderr in background threads rather than buffering
        # them for a final communicate() — lets us use proc.wait(timeout=)
        # without deadlocking on a full pipe, and means the log on disk
        # always has the scraper's most recent lines even if it's killed
        # mid-run. CLAUDE.md Open work #4.
        with open(scraper_log_path, "a", encoding="utf-8") as scraper_log_handle:
            stdout_streamer = _PipeStreamer(proc.stdout, scraper_log_handle, "OUT").start()
            stderr_streamer = _PipeStreamer(proc.stderr, scraper_log_handle, "ERR").start()
            try:
                proc.wait(timeout=scraper_timeout)
            except subprocess.TimeoutExpired as exc:
                # Don't throw away what it already wrote. The scraper streams
                # results as jobs complete, so the file usually holds most of a
                # long run; the old code deleted it in `finally` and ingested
                # nothing, turning a 30-minute partial sweep into zero rows.
                _kill_scraper_process_group(proc)
                proc.wait()
                stdout_streamer.join()
                stderr_streamer.join()
                timed_out = exc
                preserved_results_path = _preserve_results_file(results_file_path, scrape_run_id)
                preserved_log_path = _preserve_scraper_log(scraper_log_path, scrape_run_id)
                logger.error(
                    "Scraper exceeded timeout of %ds and was killed; salvaging "
                    "partial output%s%s.",
                    scraper_timeout,
                    f" (results copy at {preserved_results_path})" if preserved_results_path else "",
                    f" (log copy at {preserved_log_path})" if preserved_log_path else "",
                )
            except BaseException:
                # Ctrl-C, SIGTERM, or any other unwind while the scraper is
                # running: kill its whole process group so Chromium doesn't
                # outlive us (CLAUDE.md Open work #3). Can't help if *this*
                # process itself gets SIGKILLed — nothing in Python runs then —
                # but covers Ctrl-C and a parent that dies while still able to
                # unwind.
                _kill_scraper_process_group(proc)
                proc.wait()
                stdout_streamer.join()
                stderr_streamer.join()
                raise
            else:
                stdout_streamer.join()
                stderr_streamer.join()
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(
                        proc.returncode, cmd,
                        output=stdout_streamer.tail_text(),
                        stderr=stderr_streamer.tail_text(),
                    )
                logger.info("Scraper finished running successfully.")
        # 4. Read results and ingest into database
        ingested_count = 0
        if os.path.exists(results_file_path) and os.path.getsize(results_file_path) > 0:
            with open(results_file_path, 'r', encoding='utf-8') as rf:
                raw_text = rf.read().strip()
            # Grid mode emits JSONL (one object per line); single-centroid
            # mode emits a JSON array. Handle both.
            leads_data = []
            if raw_text.startswith("["):
                try:
                    leads_data = json.loads(raw_text)
                except json.JSONDecodeError as e:
                    # A killed run can leave the array unterminated. Per-line
                    # parsing below already tolerates a truncated tail; this
                    # branch has to say so explicitly or the salvage path
                    # raises on exactly the input it exists to handle.
                    logger.warning(
                        "Results file is a truncated JSON array (%s); "
                        "no rows recoverable from it.", e,
                    )
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
                ingested_count = len(raw_leads_to_insert)
                session.add_all(raw_leads_to_insert)
                session.commit()
                logger.info(f"Successfully ingested {ingested_count} raw leads into 'raw_leads' table.")
            else:
                logger.info("No raw leads found to ingest.")
        else:
            logger.warning("Scraper output file is empty or missing.")

        if timed_out is not None:
            # Block detection is deliberately skipped: a truncated run's yield
            # says nothing about the proxies, and scoring it would strike
            # working ones for a wall-clock problem.
            db_run.status = STATUS_TIMEOUT
            db_run.completed_at = datetime.now(timezone.utc)
            session.commit()

            if ingested_count == 0:
                # Nothing recovered, so there is nothing downstream to do.
                # Fail loudly rather than let an empty run look like a thin
                # market.
                logger.error(
                    "[%s] Scrape Run #%s marked %s with nothing salvageable.",
                    datetime.now(), scrape_run_id, STATUS_TIMEOUT,
                )
                raise timed_out

            # Salvage succeeded: swallow the timeout so the caller's
            # dedupe/crawl/export still run over what we paid for. Re-raising
            # here used to discard 342 usable leads after a 30-minute sweep
            # (2026-08-07) and forced a manual recovery every time. The
            # partial-ness is not lost — `status='timeout'` is durable, is
            # excluded from `recent_yields()`, and `run_end_to_end_pipeline`
            # reports it in the closing summary.
            logger.warning(
                "[%s] Scrape Run #%s marked %s — salvaged %d leads for %r in "
                "%r before the kill. Continuing with partial coverage; this "
                "run does NOT represent the full area.",
                datetime.now(), scrape_run_id, STATUS_TIMEOUT,
                ingested_count, query, location,
            )
            return

        # Does this yield look blocked rather than just thin?
        block_reason = _assess_run_health(
            session,
            query=query,
            location=location,
            ingested_count=ingested_count,
            session_proxies=session_proxies,
        )

        db_run.status = STATUS_BLOCKED if block_reason else STATUS_COMPLETED
        db_run.completed_at = datetime.now(timezone.utc)
        session.commit()
        if block_reason:
            logger.warning(
                "[%s] Scrape Run #%s marked %s (%s) — %d leads for %r in %r.",
                datetime.now(), scrape_run_id, STATUS_BLOCKED, block_reason,
                ingested_count, query, location,
            )
        else:
            logger.info(f"[{datetime.now()}] Completed Scrape Run #{scrape_run_id}.")
    except Exception as e:
        logger.error(f"Error during scrape/ingest: {e}")
        # The timeout branch above already recorded STATUS_TIMEOUT and
        # committed its salvage; re-raising lands here, so don't overwrite
        # that with 'failed' and lose the distinction.
        if timed_out is None:
            db_run.status = STATUS_FAILED
            db_run.completed_at = datetime.now(timezone.utc)
            session.commit()
        raise e

    finally:
        # Clean up temp files
        for path in (query_file_path, results_file_path, scraper_log_path):
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