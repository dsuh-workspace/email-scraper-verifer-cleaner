"""
Unit tests for run_pipeline CLI parsing and forwarding.
"""

import sys

import pytest

import run_pipeline


class TestParseArgs:
    def test_required_args_parse(self, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_pipeline.py",
                "--query",
                "Plumbing",
                "--location",
                "San Francisco, CA",
            ],
        )

        args = run_pipeline.parse_args()

        assert args.query == "Plumbing"
        assert args.location == "San Francisco, CA"
        assert args.min_contacts == 500
        assert args.max_depth == 20

    def test_overrides_parse(self, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_pipeline.py",
                "--query",
                "HVAC",
                "--location",
                "Plano, TX",
                "--min-contacts",
                "50",
                "--max-depth",
                "9",
            ],
        )

        args = run_pipeline.parse_args()

        assert args.query == "HVAC"
        assert args.location == "Plano, TX"
        assert args.min_contacts == 50
        assert args.max_depth == 9

    def test_missing_required_arg_exits(self, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_pipeline.py", "--query", "Plumbing"],
        )

        with pytest.raises(SystemExit):
            run_pipeline.parse_args()


class TestMain:
    def test_main_forwards_args(self, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_pipeline.py",
                "--query",
                "HVAC",
                "--location",
                "Plano, TX",
                "--min-contacts",
                "50",
                "--max-depth",
                "9",
            ],
        )

        called = {}

        def fake_run_end_to_end_pipeline(query, location, min_contacts, max_depth):
            called["query"] = query
            called["location"] = location
            called["min_contacts"] = min_contacts
            called["max_depth"] = max_depth

        monkeypatch.setattr(
            run_pipeline,
            "run_end_to_end_pipeline",
            fake_run_end_to_end_pipeline,
        )

        run_pipeline.main()

        assert called == {
            "query": "HVAC",
            "location": "Plano, TX",
            "min_contacts": 50,
            "max_depth": 9,
        }
