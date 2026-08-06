"""
Unit tests for app.pipeline.tomba_enricher:
Tomba API response parsing & database enrichment.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline import tomba_enricher


class _StubResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no JSON")
        return self._json


class TestTombaEnricher:
    @patch.object(tomba_enricher, "TOMBA_API_KEY", "mock_key")
    @patch.object(tomba_enricher, "TOMBA_SECRET_KEY", "mock_secret")
    @patch.object(tomba_enricher.requests, "get")
    def test_fetch_domain_emails_success(self, mock_get):
        mock_get.return_value = _StubResponse(
            200,
            {
                "data": {
                    "domain": "acmeplumbing.com",
                    "emails": [
                        {
                            "email": "john.doe@acmeplumbing.com",
                            "first_name": "John",
                            "last_name": "Doe",
                            "position": "Owner",
                            "phone_number": "+14085550199",
                            "score": 95,
                        }
                    ],
                }
            },
        )

        records = tomba_enricher.fetch_domain_emails_from_tomba("acmeplumbing.com")
        assert len(records) == 1
        rec = records[0]
        assert rec["email"] == "john.doe@acmeplumbing.com"
        assert rec["name"] == "John Doe"
        assert rec["title"] == "Owner"
        assert rec["phone"] == "+14085550199"

    @patch.object(tomba_enricher, "TOMBA_API_KEY", None)
    def test_fetch_domain_emails_missing_keys(self):
        records = tomba_enricher.fetch_domain_emails_from_tomba("acmeplumbing.com")
        assert records == []

    @patch.object(tomba_enricher, "TOMBA_API_KEY", "mock_key")
    @patch.object(tomba_enricher, "TOMBA_SECRET_KEY", "mock_secret")
    @patch.object(tomba_enricher.requests, "get")
    def test_fetch_domain_emails_api_error(self, mock_get):
        mock_get.return_value = _StubResponse(401, text="Unauthorized")
        records = tomba_enricher.fetch_domain_emails_from_tomba("acmeplumbing.com")
        assert records == []
