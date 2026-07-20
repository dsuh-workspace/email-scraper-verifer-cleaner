from pathlib import Path

import pytest

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
