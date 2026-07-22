import subprocess
from pathlib import Path

import pytest

from app.scraper import run_scraper
from app.scraper.run_scraper import _scraper_proxy_args


@pytest.fixture(autouse=True)
def _clear_scraper_proxy_env(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXIES", raising=False)
    monkeypatch.delenv("SCRAPER_PROXIES_FILE", raising=False)


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
        class FakeResp:
            status_code = 200

            @staticmethod
            def json():
                return [{
                    "lat": "37.336",
                    "lon": "-121.891",
                    # Nominatim returns [south, north, west, east] as strings.
                    "boundingbox": ["37.21", "37.47", "-122.05", "-121.75"],
                }]

        import requests as _requests
        monkeypatch.setattr(_requests, "get", lambda *a, **k: FakeResp())

        lat, lon, bbox = run_scraper.geocode_location("San Jose, CA")
        assert lat == pytest.approx(37.336)
        assert lon == pytest.approx(-121.891)
        # Returned bbox is (min_lat, min_lon, max_lat, max_lon).
        assert bbox == pytest.approx((37.21, -122.05, 37.47, -121.75))

    def test_returns_none_bbox_when_missing(self, monkeypatch):
        class FakeResp:
            status_code = 200

            @staticmethod
            def json():
                return [{"lat": "1", "lon": "2"}]  # no boundingbox

        import requests as _requests
        monkeypatch.setattr(_requests, "get", lambda *a, **k: FakeResp())

        lat, lon, bbox = run_scraper.geocode_location("x")
        assert (lat, lon) == (1.0, 2.0)
        assert bbox is None

    def test_returns_none_triple_on_error(self, monkeypatch):
        import requests as _requests
        def _boom(*a, **k):
            raise _requests.RequestException("network")
        monkeypatch.setattr(_requests, "get", _boom)

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
