"""
Unit tests for the pure parts of app.pipeline.verify_emails:
verify_email_via_reacher's response handling — no live server needed.
"""

from unittest.mock import patch, MagicMock

import pytest

from app.pipeline import verify_emails


class _StubResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no JSON")
        return self._json


class TestVerifyEmailViaReacher:
    def test_bad_input_no_at_sign(self):
        # Short-circuits before any network call.
        result = verify_emails.verify_email_via_reacher("not-an-email")
        assert result["is_reachable"] == "invalid"
        assert result["score"] == 0

    def test_empty_input(self):
        result = verify_emails.verify_email_via_reacher("")
        assert result["is_reachable"] == "invalid"

    @patch.object(verify_emails.requests, "post")
    def test_safe_response(self, mock_post):
        mock_post.return_value = _StubResponse(
            200, {"is_reachable": "safe"}
        )
        r = verify_emails.verify_email_via_reacher("info@acme.com")
        assert r["is_reachable"] == "safe"
        assert r["score"] == 95

    @patch.object(verify_emails.requests, "post")
    def test_risky_response(self, mock_post):
        mock_post.return_value = _StubResponse(200, {"is_reachable": "risky"})
        r = verify_emails.verify_email_via_reacher("info@acme.com")
        assert r["is_reachable"] == "risky"
        assert r["score"] == 50

    @patch.object(verify_emails.requests, "post")
    def test_invalid_response(self, mock_post):
        mock_post.return_value = _StubResponse(200, {"is_reachable": "invalid"})
        r = verify_emails.verify_email_via_reacher("info@acme.com")
        assert r["is_reachable"] == "invalid"
        assert r["score"] == 10

    @patch.object(verify_emails.requests, "post")
    def test_unknown_status_normalized(self, mock_post):
        # Reacher returned something outside our vocabulary — normalize to unknown.
        mock_post.return_value = _StubResponse(200, {"is_reachable": "weird"})
        r = verify_emails.verify_email_via_reacher("info@acme.com")
        assert r["is_reachable"] == "unknown"

    @patch.object(verify_emails.requests, "post")
    def test_non_200_falls_back_to_unknown(self, mock_post):
        mock_post.return_value = _StubResponse(500, text="server error")
        r = verify_emails.verify_email_via_reacher("info@acme.com")
        assert r["is_reachable"] == "unknown"
        assert r["score"] == 25

    @patch.object(verify_emails.requests, "post")
    def test_non_json_body_falls_back_to_unknown(self, mock_post):
        mock_post.return_value = _StubResponse(200, json_data=None)
        r = verify_emails.verify_email_via_reacher("info@acme.com")
        assert r["is_reachable"] == "unknown"

    @patch.object(verify_emails.requests, "post")
    def test_network_error_falls_back_to_unknown(self, mock_post):
        mock_post.side_effect = verify_emails.requests.exceptions.ConnectionError(
            "boom"
        )
        r = verify_emails.verify_email_via_reacher("info@acme.com")
        assert r["is_reachable"] == "unknown"
        assert r["score"] == 25
