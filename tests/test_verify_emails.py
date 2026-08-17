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


class TestCheckReacherHealth:
    @patch.object(verify_emails.requests, "post")
    def test_health_check_healthy(self, mock_post):
        mock_post.return_value = _StubResponse(200, {"is_reachable": "safe"})
        assert verify_emails.check_reacher_health() is True

    @patch.object(verify_emails.requests, "post")
    def test_health_check_unreachable(self, mock_post):
        mock_post.side_effect = verify_emails.requests.exceptions.ConnectionError("offline")
        assert verify_emails.check_reacher_health() is False

    @patch("app.pipeline.verify_emails.check_reacher_health", return_value=False)
    @patch("app.pipeline.verify_emails.TOMBA_API_KEY", None)
    @patch("app.pipeline.verify_emails.TOMBA_SECRET_KEY", None)
    @patch("sqlalchemy.orm.sessionmaker")
    def test_verify_contacts_aborts_when_no_verifier_available(self, mock_sessionmaker, mock_health):
        mock_session = MagicMock()
        mock_session.query().filter().filter().all.return_value = [MagicMock(id=1, email="test@test.com")]
        mock_sessionmaker.return_value = MagicMock(return_value=mock_session)

        with pytest.raises(RuntimeError, match="No email verifier is reachable"):
            verify_emails.verify_contacts_emails(raise_on_unreachable=True)


class TestVerifyEmailViaTomba:
    @patch.object(verify_emails.requests, "get")
    def test_tomba_deliverable_mapped_to_safe(self, mock_get):
        mock_get.return_value = _StubResponse(
            200,
            {"data": {"email": {"email": "john@doe.com", "result": "deliverable", "status": "valid", "score": 95, "accept_all": False}}}
        )
        res = verify_emails.verify_email_via_tomba("john@doe.com")
        assert res["is_reachable"] == "safe"
        assert res["score"] >= 90

    @patch.object(verify_emails.requests, "get")
    def test_tomba_undeliverable_mapped_to_invalid(self, mock_get):
        mock_get.return_value = _StubResponse(
            200,
            {"data": {"email": {"email": "bad@fake.com", "result": "undeliverable", "status": "invalid", "score": 10}}}
        )
        res = verify_emails.verify_email_via_tomba("bad@fake.com")
        assert res["is_reachable"] == "invalid"
        assert res["score"] <= 15


