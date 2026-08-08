"""End-to-end wiring for block detection.

The pieces are unit-tested in `test_proxy_health.py`; this covers the chain
through `execute_scrape_and_ingest`: scraper output -> ingest -> yield verdict
-> `scrape_runs.status` -> proxy ledger. Uses a real SQLite DB so the status
and the history query are exercised for real.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.create_tables import Base, RawLead, ScrapeRun
from app.scraper import run_scraper
from app.scraper.proxy_health import proxy_id

P1 = "http://u1:pw1@p1.example.com:8080"
P2 = "http://u2:pw2@p2.example.com:8080"

LEAD = {
    "title": "Acme Plumbing",
    "category": "Plumber",
    "phone": "+1 408-555-0100",
    "website": "https://acme-plumbing.example.com",
    "review_count": 12,
    "review_rating": 4.6,
    "address": "1 Main St, San Jose, CA",
    "status": "OPEN",
    "description": "Plumbing services",
    "place_id": "place-1",
}


@pytest.fixture
def db_session_factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(run_scraper, "Session", factory)
    return factory


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("PROXY_HEALTH_FILE", str(tmp_path / "proxy_health.json"))
    monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2}")
    monkeypatch.delenv("SCRAPER_PROXIES_FILE", raising=False)
    monkeypatch.delenv("SCRAPER_PACING_SEC", raising=False)
    # No real waiting in tests; the wait path has its own test.
    monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "0")
    for name in ("BLOCK_DETECT_ENABLED", "BLOCK_DETECT_ZERO_YIELD",
                 "BLOCK_DETECT_MIN_HISTORY", "BLOCK_DETECT_LOW_YIELD_RATIO"):
        monkeypatch.delenv(name, raising=False)


def _stub_scraper(monkeypatch, tmp_path, leads: list[dict]):
    """Make the scraper subprocess a no-op that writes `leads` as its output."""
    monkeypatch.setattr(run_scraper, "_scraper_binary_path", lambda: str(tmp_path / "bin"))
    (tmp_path / "bin").write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        run_scraper, "geocode_location", lambda location: (37.3, -121.9, None)
    )

    class _FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            self.pid = 99999
            self.returncode = 0
            if "-results" not in cmd:
                # The stale-process check also routes through
                # subprocess.run -> Popen and hits this fake.
                return
            results_path = cmd[cmd.index("-results") + 1]
            with open(results_path, "w", encoding="utf-8") as handle:
                json.dump(leads, handle)

        def communicate(self, timeout=None):
            return ("", "")

        def wait(self, timeout=None):
            return self.returncode

    import subprocess as _sp
    monkeypatch.setattr(_sp, "Popen", _FakePopen)


def _run(query="Plumbing", location="San Jose, CA"):
    run_scraper.execute_scrape_and_ingest(query, location, lat=37.3, lon=-121.9)


def _statuses(factory):
    session = factory()
    try:
        return [
            (r.query, r.status)
            for r in session.query(ScrapeRun).order_by(ScrapeRun.id).all()
        ]
    finally:
        session.close()


class TestBlockDetectionWiring:
    def test_zero_yield_marks_run_blocked_and_strikes_proxies(
        self, monkeypatch, tmp_path, db_session_factory
    ):
        _stub_scraper(monkeypatch, tmp_path, [])

        _run()

        assert _statuses(db_session_factory) == [("Plumbing", "blocked")]

        ledger = json.loads((tmp_path / "proxy_health.json").read_text())
        struck = set(ledger)
        # Only the proxies actually forwarded to this invocation are charged.
        assert struck and struck <= {proxy_id(P1), proxy_id(P2)}
        assert all(entry["strikes"] == 1 for entry in ledger.values())
        assert all(entry["reason"] == "zero-yield" for entry in ledger.values())

    def test_healthy_yield_completes_and_writes_no_strikes(
        self, monkeypatch, tmp_path, db_session_factory
    ):
        _stub_scraper(monkeypatch, tmp_path, [LEAD])

        _run()

        assert _statuses(db_session_factory) == [("Plumbing", "completed")]
        assert not (tmp_path / "proxy_health.json").exists()

        session = db_session_factory()
        try:
            assert session.query(RawLead).count() == 1
        finally:
            session.close()

    def test_leads_are_still_ingested_when_a_run_is_flagged(
        self, monkeypatch, tmp_path, db_session_factory
    ):
        # Build history of 3 healthy runs, then a collapsed one.
        _stub_scraper(monkeypatch, tmp_path, [LEAD | {"place_id": f"p{i}"} for i in range(20)])
        for _ in range(3):
            _run()

        _stub_scraper(monkeypatch, tmp_path, [LEAD | {"place_id": "collapsed"}])
        _run()

        statuses = _statuses(db_session_factory)
        assert [s for _, s in statuses] == ["completed"] * 3 + ["blocked"]

        session = db_session_factory()
        try:
            # The flag is a signal, not a gate — the lead is still in the DB.
            assert session.query(RawLead).filter_by(place_id="collapsed").count() == 1
        finally:
            session.close()

    def test_blocked_runs_are_excluded_from_the_baseline(
        self, monkeypatch, tmp_path, db_session_factory
    ):
        # Wider pool than the default proxy limit of 3, so the strikes from the
        # first collapsed run leave something for the second run to use.
        monkeypatch.setenv(
            "SCRAPER_PROXIES",
            ",".join(f"http://u{i}:pw{i}@p{i}.example.com:8080" for i in range(1, 6)),
        )
        _stub_scraper(monkeypatch, tmp_path, [LEAD | {"place_id": f"p{i}"} for i in range(20)])
        for _ in range(3):
            _run()

        # Two collapsed runs in a row. If blocked runs counted toward the
        # median, the second would look normal against the first and the
        # detector would go quiet exactly when it matters.
        _stub_scraper(monkeypatch, tmp_path, [LEAD | {"place_id": "c1"}])
        _run()
        _stub_scraper(monkeypatch, tmp_path, [LEAD | {"place_id": "c2"}])
        _run()

        assert [s for _, s in _statuses(db_session_factory)][-2:] == ["blocked", "blocked"]

    def test_detection_disabled_leaves_runs_completed(
        self, monkeypatch, tmp_path, db_session_factory
    ):
        monkeypatch.setenv("BLOCK_DETECT_ENABLED", "0")
        _stub_scraper(monkeypatch, tmp_path, [])

        _run()

        assert _statuses(db_session_factory) == [("Plumbing", "completed")]
        assert not (tmp_path / "proxy_health.json").exists()

    def test_unproxied_run_is_never_flagged(
        self, monkeypatch, tmp_path, db_session_factory
    ):
        # Zero leads with no proxy is a different problem; nothing to charge.
        _stub_scraper(monkeypatch, tmp_path, [])

        run_scraper.execute_scrape_and_ingest(
            "Plumbing", "San Jose, CA", lat=37.3, lon=-121.9, disable_proxy=True
        )

        assert _statuses(db_session_factory) == [("Plumbing", "completed")]
        assert not (tmp_path / "proxy_health.json").exists()

    def test_history_is_scoped_per_query_and_location(
        self, monkeypatch, tmp_path, db_session_factory
    ):
        _stub_scraper(monkeypatch, tmp_path, [LEAD | {"place_id": f"p{i}"} for i in range(20)])
        for _ in range(3):
            _run(query="Plumbing", location="San Jose, CA")

        # A thin first run for a *different* location must not be judged
        # against San Jose's baseline.
        _stub_scraper(monkeypatch, tmp_path, [LEAD | {"place_id": "other"}])
        _run(query="Plumbing", location="Gilroy, CA")

        statuses = dict(
            (loc, status)
            for (loc, status) in [
                (r.location, r.status)
                for r in db_session_factory().query(ScrapeRun).all()
            ]
        )
        assert statuses["Gilroy, CA"] == "completed"
