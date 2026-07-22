"""
Unit tests for the pure helpers in app.pipeline.process_leads:

    - extract_domain: strip scheme, www., port, lowercase
    - normalize_phone: E.164 conversion + fallbacks
    - _parse_and_validate_emails: multi-delimiter split + regex filter

These are the deterministic, side-effect-free functions — the DB-heavy
process_and_deduplicate_leads is skipped here on purpose (integration
test territory).
"""

from app.pipeline.process_leads import (
    _parse_and_validate_emails,
    extract_domain,
    normalize_phone,
)


class TestExtractDomain:
    def test_bare_url(self):
        assert extract_domain("rotorooter.com") == "rotorooter.com"

    def test_http_scheme(self):
        assert extract_domain("http://rotorooter.com") == "rotorooter.com"

    def test_https_scheme(self):
        assert extract_domain("https://rotorooter.com") == "rotorooter.com"

    def test_www_prefix_stripped(self):
        assert extract_domain("https://www.rotorooter.com") == "rotorooter.com"

    def test_uppercase_lowercased(self):
        assert extract_domain("HTTPS://Rotorooter.COM") == "rotorooter.com"

    def test_port_stripped(self):
        assert extract_domain("http://example.com:8080/path") == "example.com"

    def test_trailing_path_ignored(self):
        assert extract_domain("https://acme.com/contact-us") == "acme.com"

    def test_none_returns_none(self):
        assert extract_domain(None) is None

    def test_empty_string_returns_none(self):
        assert extract_domain("") is None

    def test_whitespace_stripped(self):
        assert extract_domain("  https://acme.com  ") == "acme.com"


class TestNormalizePhone:
    def test_ten_digits(self):
        assert normalize_phone("415-555-1212") == "+14155551212"

    def test_with_parens_and_dashes(self):
        assert normalize_phone("(415) 555-1212") == "+14155551212"

    def test_eleven_digits_leading_one(self):
        assert normalize_phone("1-415-555-1212") == "+14155551212"

    def test_already_e164(self):
        # Function normalizes any 11-digit-starting-1 blob to +... form.
        assert normalize_phone("+1 415 555 1212") == "+14155551212"

    def test_none_returns_none(self):
        assert normalize_phone(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_phone("") is None

    def test_non_us_length_returned_as_is(self):
        # Function returns the stripped original when digit count isn't 10/11.
        assert normalize_phone("+44 20 7946 0958") == "+44 20 7946 0958"

    def test_letters_stripped_for_count(self):
        # 10 digits after non-digit strip → normalized
        assert normalize_phone("call 4155551212 today") == "+14155551212"


class TestParseAndValidateEmails:
    def test_single_email(self):
        assert _parse_and_validate_emails("foo@bar.com") == ["foo@bar.com"]

    def test_comma_separated(self):
        assert _parse_and_validate_emails("a@x.com,b@y.com") == [
            "a@x.com", "b@y.com",
        ]

    def test_semicolon_separated(self):
        assert _parse_and_validate_emails("a@x.com;b@y.com") == [
            "a@x.com", "b@y.com",
        ]

    def test_whitespace_separated(self):
        assert _parse_and_validate_emails("a@x.com b@y.com") == [
            "a@x.com", "b@y.com",
        ]

    def test_mixed_delimiters(self):
        assert _parse_and_validate_emails("a@x.com, b@y.com; c@z.com") == [
            "a@x.com", "b@y.com", "c@z.com",
        ]

    def test_invalid_dropped(self):
        assert _parse_and_validate_emails("valid@x.com, not-an-email") == [
            "valid@x.com",
        ]

    def test_lowercased(self):
        assert _parse_and_validate_emails("Foo@Bar.COM") == ["foo@bar.com"]

    def test_dedup(self):
        # Duplicate address after normalization → deduped.
        assert _parse_and_validate_emails("Foo@Bar.com, foo@bar.com") == [
            "foo@bar.com",
        ]

    def test_industry_tld_kept(self):
        # The reason we loosened the regex from {2,4} to {2,}.
        assert _parse_and_validate_emails("info@joe.plumbing") == [
            "info@joe.plumbing",
        ]
        assert _parse_and_validate_emails("hello@acme.services") == [
            "hello@acme.services",
        ]

    def test_none_returns_empty(self):
        assert _parse_and_validate_emails(None) == []

    def test_empty_returns_empty(self):
        assert _parse_and_validate_emails("") == []

    def test_trailing_punctuation_stripped(self):
        assert _parse_and_validate_emails("info@acme.com.") == ["info@acme.com"]
