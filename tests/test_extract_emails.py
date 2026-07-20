"""
Unit tests for app.pipeline.extract_emails.extract_emails_from_html —
the pure HTML → email-list function.
"""

from app.pipeline.extract_emails import extract_emails_from_html


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
