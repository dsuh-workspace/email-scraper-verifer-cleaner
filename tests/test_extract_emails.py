"""
Unit tests for app.pipeline.extract_emails helpers.
"""

import pytest

from app.pipeline.extract_emails import (
    _build_crawler_proxies,
    extract_emails_from_html,
)


class TestExtractEmailsFromHtml:
    def test_simple_mailto(self):
        html = '<a href="mailto:info@acme.com">Contact</a>'
        assert extract_emails_from_html(html) == ["info@acme.com"]

    def test_multiple_unique(self):
        html = (
            'Reach us at hello@acme.com or sales@acme.com. '
            'Also try hello@acme.com again.'
        )
        # Sorted, deduped.
        assert extract_emails_from_html(html) == [
            "hello@acme.com", "sales@acme.com",
        ]

    def test_lowercased(self):
        html = "Contact: HELLO@Acme.COM"
        assert extract_emails_from_html(html) == ["hello@acme.com"]

    def test_industry_tld_captured(self):
        html = "Reach us: info@joe.plumbing"
        assert extract_emails_from_html(html) == ["info@joe.plumbing"]

    def test_image_filename_excluded(self):
        # image@foo.jpg shouldn't be counted as an email
        html = "<img src=\"foo.jpg\"> contact: real@acme.com"
        emails = extract_emails_from_html(html)
        assert "real@acme.com" in emails
        assert not any(e.endswith(".jpg") for e in emails)

    def test_sentry_dsn_excluded(self):
        html = (
            'Sentry.init({dsn: "abc123@o12345.ingest.sentry.io"});'
            ' contact: real@acme.com'
        )
        emails = extract_emails_from_html(html)
        assert "real@acme.com" in emails
        assert not any("sentry.io" in e for e in emails)

    def test_empty_html(self):
        assert extract_emails_from_html("") == []

    def test_no_emails_present(self):
        assert extract_emails_from_html("<html>no contact info</html>") == []

    def test_return_sorted(self):
        html = "zeta@acme.com alpha@acme.com beta@acme.com"
        assert extract_emails_from_html(html) == [
            "alpha@acme.com", "beta@acme.com", "zeta@acme.com",
        ]


@pytest.fixture(autouse=True)
def _clear_crawler_proxy_env(monkeypatch):
    monkeypatch.delenv("CRAWLER_PROXY", raising=False)
    monkeypatch.delenv("CRAWLER_HTTP_PROXY", raising=False)
    monkeypatch.delenv("CRAWLER_HTTPS_PROXY", raising=False)
    monkeypatch.delenv("CRAWLER_PROXY_FILE", raising=False)


class TestBuildCrawlerProxies:
    def test_returns_none_when_unset(self):
        assert _build_crawler_proxies() is None

    def test_uses_fallback_for_both_schemes(self, monkeypatch):
        monkeypatch.setenv("CRAWLER_PROXY", "http://proxy.example.com:8080")

        assert _build_crawler_proxies() == {
            "http": "http://proxy.example.com:8080",
            "https": "http://proxy.example.com:8080",
        }

    def test_prefers_split_values(self, monkeypatch):
        monkeypatch.setenv("CRAWLER_PROXY", "http://fallback.example.com:8080")
        monkeypatch.setenv("CRAWLER_HTTP_PROXY", "http://http.example.com:8081")
        monkeypatch.setenv("CRAWLER_HTTPS_PROXY", "https://https.example.com:8443")

        assert _build_crawler_proxies() == {
            "http": "http://http.example.com:8081",
            "https": "https://https.example.com:8443",
        }

    def test_rejects_invalid_scheme(self, monkeypatch):
        monkeypatch.setenv("CRAWLER_PROXY", "socks5://proxy.example.com:1080")
        monkeypatch.delenv("CRAWLER_HTTP_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_HTTPS_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_PROXY_FILE", raising=False)

        with pytest.raises(ValueError, match="Unsupported crawler proxy scheme"):
            _build_crawler_proxies()

    def test_uses_first_proxy_from_file(self, monkeypatch, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "# comment\nhttp://proxy1.example.com:8080\nhttp://proxy2.example.com:8081\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("CRAWLER_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_HTTP_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_HTTPS_PROXY", raising=False)
        monkeypatch.setenv("CRAWLER_PROXY_FILE", str(proxy_file))

        assert _build_crawler_proxies() == {
            "http": "http://proxy1.example.com:8080",
            "https": "http://proxy1.example.com:8080",
        }
