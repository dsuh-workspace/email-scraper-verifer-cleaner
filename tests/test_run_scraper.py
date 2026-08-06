import subprocess
from pathlib import Path

import pytest

from app.scraper import run_scraper
from app.scraper.run_scraper import _scraper_proxy_args


@pytest.fixture(autouse=True)
def _clear_scraper_proxy_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SCRAPER_PROXIES", raising=False)
    monkeypatch.delenv("SCRAPER_PROXIES_FILE", raising=False)
    monkeypatch.delenv("SCRAPER_PROXY_LIMIT", raising=False)
    monkeypatch.delenv("SCRAPER_CONCURRENCY", raising=False)
    monkeypatch.delenv("SCRAPER_BROWSER_POOL_SIZE", raising=False)
    monkeypatch.delenv("SCRAPER_PAGES_PER_BROWSER", raising=False)
    monkeypatch.delenv("SCRAPER_PACING_SEC", raising=False)
    # Point the proxy health ledger at a throwaway file: tests must not read a
    # real cooldown (which would hide proxies) or write strikes to real state.
    monkeypatch.setenv("PROXY_HEALTH_FILE", str(tmp_path / "proxy_health.json"))
    monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "0")  # never sleep in tests


class TestScraperProxyArgs:
    def test_returns_empty_when_unset(self):
        assert _scraper_proxy_args() == []

    def test_returns_upstream_flag_for_single_proxy(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", "http://proxy.example.com:8080")

        assert _scraper_proxy_args() == [
            "-proxies",
            "http://proxy.example.com:8080",
        ]

    def test_preserves_multiple_proxy_values(self, monkeypatch):
        monkeypatch.setenv(
            "SCRAPER_PROXIES",
            "http://proxy1.example.com:8080, socks5://proxy2.example.com:1080",
        )

        assert _scraper_proxy_args() == [
            "-proxies",
            "http://proxy1.example.com:8080,socks5://proxy2.example.com:1080",
        ]

    def test_rejects_empty_segment(self, monkeypatch):
        monkeypatch.setenv(
            "SCRAPER_PROXIES",
            "http://proxy1.example.com:8080,,http://proxy2.example.com:8081",
        )

        with pytest.raises(ValueError, match="empty entry"):
            _scraper_proxy_args()

    def test_rejects_unsupported_scheme(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", "ftp://proxy.example.com:21")

        with pytest.raises(ValueError, match="Unsupported proxy scheme"):
            _scraper_proxy_args()

    def test_loads_proxy_file(self, monkeypatch, tmp_path: Path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "# comment\nhttp://proxy1.example.com:8080\nhttp://proxy2.example.com:8081\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("SCRAPER_PROXIES", raising=False)
        monkeypatch.setenv("SCRAPER_PROXIES_FILE", str(proxy_file))

        assert _scraper_proxy_args() == [
            "-proxies",
            "http://proxy1.example.com:8080,http://proxy2.example.com:8081",
        ]

    def test_loads_compact_webshare_proxy_file(self, monkeypatch, tmp_path: Path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "p.webshare.io:80:user1:pass1\np.webshare.io:80:user2:pass2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SCRAPER_PROXIES_FILE", str(proxy_file))

        assert _scraper_proxy_args() == [
            "-proxies",
            "http://user1:pass1@p.webshare.io:80,http://user2:pass2@p.webshare.io:80",
        ]

    def test_disable_proxy_short_circuits(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", "http://proxy.example.com:8080")

        assert _scraper_proxy_args(disable_proxy=True) == []


class TestGeocodeReturnsBbox:
    def test_returns_bbox_tuple_from_nominatim(self, monkeypatch):
        class FakeLocation:
            latitude = "37.336"
            longitude = "-121.891"
            raw = {
                "boundingbox": ["37.21", "37.47", "-122.05", "-121.75"],
            }

        class FakeGeocoder:
            def __init__(self, *args, **kwargs):
                pass

            def geocode(self, *args, **kwargs):
                return FakeLocation()

        monkeypatch.setattr(run_scraper, "Nominatim", FakeGeocoder)

        lat, lon, bbox = run_scraper.geocode_location("San Jose, CA")
        assert lat == pytest.approx(37.336)
        assert lon == pytest.approx(-121.891)
        # Returned bbox is (min_lat, min_lon, max_lat, max_lon).
        assert bbox == pytest.approx((37.21, -122.05, 37.47, -121.75))

    def test_returns_none_bbox_when_missing(self, monkeypatch):
        class FakeLocation:
            latitude = "1"
            longitude = "2"
            raw = {}

        class FakeGeocoder:
            def __init__(self, *args, **kwargs):
                pass

            def geocode(self, *args, **kwargs):
                return FakeLocation()

        monkeypatch.setattr(run_scraper, "Nominatim", FakeGeocoder)

        lat, lon, bbox = run_scraper.geocode_location("x")
        assert (lat, lon) == (1.0, 2.0)
        assert bbox is None

    def test_returns_none_triple_on_error(self, monkeypatch):
        class FakeGeocoder:
            def __init__(self, *args, **kwargs):
                pass

            def geocode(self, *args, **kwargs):
                raise RuntimeError("network")

        monkeypatch.setattr(run_scraper, "Nominatim", FakeGeocoder)

        assert run_scraper.geocode_location("x") == (None, None, None)


class TestExecuteScrapeGridCli:
    """
    Grid-mode branch of execute_scrape_and_ingest: verifies subprocess CLI
    construction. Actual scraper subprocess is stubbed.
    """

    def _stub_all(self, monkeypatch, captured: list):
        # Bypass DB entirely.
        class _FakeSession:
            def add(self, *_): pass
            def commit(self): pass
            def query(self, *_): return self
            def filter_by(self, **_): return self
            def first(self): return None
            def close(self): pass
        monkeypatch.setattr(run_scraper, "Session", lambda: _FakeSession())

        # Skip real geocode.
        monkeypatch.setattr(
            run_scraper, "geocode_location",
            lambda location: (None, None, None),
        )

        # Capture subprocess call and skip the actual scraper.
        class _Completed:
            returncode = 0
            stderr = ""
        def fake_run(cmd, *args, **kwargs):
            captured.append(cmd)
            return _Completed()
        monkeypatch.setattr(subprocess, "run", fake_run)

        # Stub scraper binary path (skip real binary requirement) and short-
        # circuit the results-file exists check so parse block is skipped.
        monkeypatch.setattr(run_scraper, "_scraper_binary_path", lambda: "/tmp/fake-scraper-bin")
        import os as _os
        real_exists = _os.path.exists
        def _fake_exists(p):
            if p == "/tmp/fake-scraper-bin":
                return True
            if isinstance(p, str) and p.endswith(".json"):
                return False
            return real_exists(p)
        monkeypatch.setattr(_os.path, "exists", _fake_exists)

    def test_grid_mode_adds_grid_flags_and_drops_fast_mode(self, monkeypatch, tmp_path):
        captured = []
        self._stub_all(monkeypatch, captured)

        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            bbox=(37.21, -122.05, 37.47, -121.75),
            cell_km=2.0,
        )

        assert captured, "subprocess.run was not called"
        cmd = captured[0]
        assert "-grid-bbox" in cmd
        i = cmd.index("-grid-bbox")
        assert cmd[i + 1] == "37.21,-122.05,37.47,-121.75"
        assert "-grid-cell" in cmd
        j = cmd.index("-grid-cell")
        assert cmd[j + 1] == "2.0"
        # Grid mode: -fast-mode must NOT be present (scraper rejects the combo).
        assert "-fast-mode" not in cmd
        # -geo must NOT be present in grid mode.
        assert "-geo" not in cmd

    def test_single_centroid_keeps_fast_mode(self, monkeypatch):
        captured = []
        self._stub_all(monkeypatch, captured)

        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            lat=37.3,
            lon=-121.9,
        )

        assert captured
        cmd = captured[0]
        assert "-fast-mode" in cmd
        assert "-geo" in cmd
        assert "-grid-bbox" not in cmd
        assert "-grid-cell" not in cmd


class TestExecuteScrapeMultiQuery:
    """Multi-query mode: N queries in one input file, browser context reuse."""

    def _stub_all(self, monkeypatch, captured: list, query_files: list):
        class _FakeSession:
            def add(self, *_): pass
            def commit(self): pass
            def query(self, *_): return self
            def filter_by(self, **_): return self
            def first(self): return None
            def close(self): pass
        monkeypatch.setattr(run_scraper, "Session", lambda: _FakeSession())
        monkeypatch.setattr(
            run_scraper, "geocode_location",
            lambda location: (None, None, None),
        )

        class _Completed:
            returncode = 0
            stderr = ""

        # Capture cmd AND the query-file contents (subprocess.run picks the
        # -input arg out of cmd, we snapshot it here before tmpfile is cleaned).
        real_open = open
        def fake_run(cmd, *args, **kwargs):
            captured.append(cmd)
            # cmd is [binary, "-input", <path>, "-results", ...]
            try:
                idx = cmd.index("-input")
                query_path = cmd[idx + 1]
                with real_open(query_path, "r", encoding="utf-8") as f:
                    query_files.append(f.read())
            except (ValueError, FileNotFoundError):
                query_files.append(None)
            return _Completed()
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", fake_run)

        monkeypatch.setattr(run_scraper, "_scraper_binary_path", lambda: "/tmp/fake-scraper-bin")
        import os as _os
        real_exists = _os.path.exists
        def _fake_exists(p):
            if p == "/tmp/fake-scraper-bin":
                return True
            if isinstance(p, str) and p.endswith(".json"):
                return False
            return real_exists(p)
        monkeypatch.setattr(_os.path, "exists", _fake_exists)

    def test_multi_query_writes_all_lines_to_input(self, monkeypatch):
        captured, files = [], []
        self._stub_all(monkeypatch, captured, files)
        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            lat=37.3,
            lon=-121.9,
            queries=["Plumbing", "Plumber", "Leak repair"],
            fast_mode=False,
            depth=10,
        )
        assert len(captured) == 1, "expected exactly one subprocess call"
        assert files[0] is not None
        lines = [ln for ln in files[0].splitlines() if ln.strip()]
        assert lines == [
            "Plumbing in San Jose, CA",
            "Plumber in San Jose, CA",
            "Leak repair in San Jose, CA",
        ]
        cmd = captured[0]
        # fast_mode=False should NOT add -fast-mode.
        assert "-fast-mode" not in cmd
        # -geo present since bbox is None.
        assert "-geo" in cmd
        # -lang defaulted to 'en'.
        assert "-lang" in cmd
        assert cmd[cmd.index("-lang") + 1] == "en"

    def test_fast_mode_true_with_bbox_raises(self, monkeypatch):
        captured, files = [], []
        self._stub_all(monkeypatch, captured, files)
        with pytest.raises(ValueError, match="incompatible"):
            run_scraper.execute_scrape_and_ingest(
                query="Plumbing",
                location="San Jose, CA",
                bbox=(37.21, -122.05, 37.47, -121.75),
                cell_km=2.0,
                fast_mode=True,   # explicit conflict
            )

    def test_lang_flag_customizable(self, monkeypatch):
        captured, files = [], []
        self._stub_all(monkeypatch, captured, files)
        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            lat=37.3,
            lon=-121.9,
            lang="es",
        )
        cmd = captured[0]
        assert cmd[cmd.index("-lang") + 1] == "es"

    def test_empty_queries_list_falls_back_to_query_arg(self, monkeypatch):
        captured, files = [], []
        self._stub_all(monkeypatch, captured, files)
        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            lat=37.3,
            lon=-121.9,
            queries=[],   # empty
        )
        lines = [ln for ln in files[0].splitlines() if ln.strip()]
        assert lines == ["Plumbing in San Jose, CA"]

    def test_scraper_tuning_flags_forwarded(self, monkeypatch):
        captured, files = [], []
        self._stub_all(monkeypatch, captured, files)
        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            lat=37.3,
            lon=-121.9,
            concurrency=3,
            browser_pool_size=2,
            pages_per_browser=1,
        )
        cmd = captured[0]
        assert cmd[cmd.index("-c") + 1] == "3"
        assert cmd[cmd.index("-browser-pool-size") + 1] == "2"
        assert cmd[cmd.index("-pages-per-browser") + 1] == "1"

    def test_scraper_tuning_env_defaults_forwarded(self, monkeypatch):
        captured, files = [], []
        self._stub_all(monkeypatch, captured, files)
        monkeypatch.setenv("SCRAPER_CONCURRENCY", "4")
        monkeypatch.setenv("SCRAPER_BROWSER_POOL_SIZE", "2")
        monkeypatch.setenv("SCRAPER_PAGES_PER_BROWSER", "1")
        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            lat=37.3,
            lon=-121.9,
        )
        cmd = captured[0]
        assert cmd[cmd.index("-c") + 1] == "4"
        assert cmd[cmd.index("-browser-pool-size") + 1] == "2"
        assert cmd[cmd.index("-pages-per-browser") + 1] == "1"

    def test_proxy_limit_trims_proxy_list(self, monkeypatch):
        monkeypatch.setenv(
            "SCRAPER_PROXIES",
            "http://proxy1.example.com:8080,http://proxy2.example.com:8080,http://proxy3.example.com:8080",
        )
        assert run_scraper._scraper_proxy_args(proxy_limit=2) == [
            "-proxies",
            "http://proxy1.example.com:8080,http://proxy2.example.com:8080",
        ]

    def test_default_proxy_limit_is_three(self, monkeypatch):
        monkeypatch.setenv(
            "SCRAPER_PROXIES",
            "http://proxy1.example.com:8080,http://proxy2.example.com:8080,http://proxy3.example.com:8080,http://proxy4.example.com:8080",
        )
        assert run_scraper._scraper_proxy_args() == [
            "-proxies",
            "http://proxy1.example.com:8080,http://proxy2.example.com:8080,http://proxy3.example.com:8080",
        ]

    def test_proxy_limit_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv(
            "SCRAPER_PROXIES",
            "http://proxy1.example.com:8080,http://proxy2.example.com:8080,http://proxy3.example.com:8080,http://proxy4.example.com:8080",
        )
        monkeypatch.setenv("SCRAPER_PROXY_LIMIT", "4")
        assert run_scraper._scraper_proxy_args() == [
            "-proxies",
            "http://proxy1.example.com:8080,http://proxy2.example.com:8080,http://proxy3.example.com:8080,http://proxy4.example.com:8080",
        ]

    def test_disable_page_reuse_forwards_flag(self, monkeypatch):
        captured, files = [], []
        self._stub_all(monkeypatch, captured, files)
        run_scraper.execute_scrape_and_ingest(
            query="Plumbing",
            location="San Jose, CA",
            lat=37.3,
            lon=-121.9,
            disable_page_reuse=True,
        )
        assert "-disable-page-reuse" in captured[0]

    def test_invalid_scraper_tuning_values_raise(self, monkeypatch):
        with pytest.raises(ValueError, match="SCRAPER_CONCURRENCY must be > 0"):
            run_scraper.execute_scrape_and_ingest(
                query="Plumbing",
                location="San Jose, CA",
                lat=37.3,
                lon=-121.9,
                concurrency=0,
            )
