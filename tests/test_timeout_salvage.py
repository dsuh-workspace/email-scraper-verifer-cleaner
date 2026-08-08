"""A killed scraper run keeps what it already wrote.

`subprocess.run(timeout=...)` discards stdout/stderr, and the `finally` block
deletes the `-results` tempfile, so a timeout used to leave nothing at all: no
rows, no evidence of how far the sweep got. Two San Jose grid runs on
2026-08-07 died this way at 1800s with `raw_leads = 0`.

The scraper streams results as jobs complete, so that file normally holds most
of a long run. These cover: the partial rows get ingested, the run is recorded
as `timeout` rather than `failed` or `completed`, and a copy of the file
survives under `logs/`.

Whether the TimeoutExpired propagates depends on whether anything was
salvaged. With rows recovered it is swallowed so the caller's dedupe / crawl /
export still run — re-raising discarded 342 usable leads after a 30-minute
sweep on 2026-08-07. With nothing recovered it propagates, so an empty run
never passes for a thin market.
"""

import io
import json
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.create_tables import Base, RawLead, ScrapeRun
from app.scraper import run_scraper
from app.scraper.block_detect import STATUS_TIMEOUT

LEADS = [
    {"title": "Acme Plumbing", "phone": "+1 408-555-0100",
     "website": "https://acme.example.com", "place_id": "p1"},
    {"title": "Bolt Rooter", "phone": "+1 408-555-0101",
     "website": "https://bolt.example.com", "place_id": "p2"},
]


@pytest.fixture
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    made = sessionmaker(bind=engine)
    monkeypatch.setattr(run_scraper, "Session", made)
    return made


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    # No proxies: block detection is skipped without them, keeping these
    # tests about the timeout path alone.
    monkeypatch.delenv("SCRAPER_PROXIES", raising=False)
    monkeypatch.delenv("SCRAPER_PROXIES_FILE", raising=False)
    monkeypatch.delenv("SCRAPER_PACING_SEC", raising=False)
    monkeypatch.setenv("PROXY_HEALTH_FILE", str(tmp_path / "proxy_health.json"))
    # logs/ is relative, so run from a scratch cwd rather than the repo.
    monkeypatch.chdir(tmp_path)


def _stub_timeout(monkeypatch, tmp_path, written: str):
    """Scraper writes `written` to -results, then is killed by the timeout."""
    monkeypatch.setattr(run_scraper, "_scraper_binary_path", lambda: str(tmp_path / "bin"))
    (tmp_path / "bin").write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        run_scraper, "geocode_location", lambda location: (37.3, -121.9, None)
    )

    class _FakePopen:
        """Writes `written` to -results, then blocks until killed by the
        timeout — pid is fake, so the process-group kill in
        `_kill_scraper_process_group` best-effort no-ops on it, same as it
        would against an already-reaped real process.

        `wait()` is what the scraper invocation itself now uses (was
        `communicate()` pre-Popen-streaming); `communicate()` stays as a
        harmless no-op since the stale-process pgrep check still routes
        through `subprocess.run()`, which calls it internally.
        """

        def __init__(self, cmd, *args, **kwargs):
            self.cmd = cmd
            self.pid = 999999
            self.returncode = None
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self._waited_once = False
            results_path = cmd[cmd.index("-results") + 1]
            with open(results_path, "w", encoding="utf-8") as handle:
                handle.write(written)

        def communicate(self, timeout=None):
            return ("", "")

        def wait(self, timeout=None):
            if not self._waited_once:
                self._waited_once = True
                raise subprocess.TimeoutExpired(self.cmd, timeout or 1800)
            self.returncode = -9
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)


def _jsonl(leads) -> str:
    return "\n".join(json.dumps(lead) for lead in leads) + "\n"


class TestTimeoutSalvage:
    def test_partial_jsonl_is_ingested_and_run_marked_timeout(
        self, factory, monkeypatch, tmp_path
    ):
        _stub_timeout(monkeypatch, tmp_path, _jsonl(LEADS))

        # Salvage succeeded, so the timeout is swallowed and the caller's
        # dedupe/crawl/export still get to run over what the sweep paid for.
        run_scraper.execute_scrape_and_ingest(
            "Plumbing", "San Jose, CA", lat=37.3, lon=-121.9,
        )

        session = factory()
        try:
            assert session.query(RawLead).count() == 2
            run = session.query(ScrapeRun).one()
            assert run.status == STATUS_TIMEOUT
            assert run.completed_at is not None
        finally:
            session.close()

    def test_truncated_final_line_still_salvages_the_rest(
        self, factory, monkeypatch, tmp_path
    ):
        """A kill mid-write leaves a partial last line; earlier ones stand."""
        _stub_timeout(monkeypatch, tmp_path, _jsonl(LEADS) + '{"title": "Half W')

        run_scraper.execute_scrape_and_ingest(
            "Plumbing", "San Jose, CA", lat=37.3, lon=-121.9,
        )

        session = factory()
        try:
            assert session.query(RawLead).count() == 2
        finally:
            session.close()

    def test_truncated_json_array_does_not_raise(
        self, factory, monkeypatch, tmp_path
    ):
        """Single-centroid mode emits an array; a killed one is unterminated.

        Nothing is recoverable from it, but the salvage path must not blow up
        on exactly the input it exists to handle — the TimeoutExpired has to
        be what surfaces.
        """
        _stub_timeout(monkeypatch, tmp_path, '[{"title": "Acme"}, {"title": "Bo')

        with pytest.raises(subprocess.TimeoutExpired):
            run_scraper.execute_scrape_and_ingest(
                "Plumbing", "San Jose, CA", lat=37.3, lon=-121.9,
            )

        session = factory()
        try:
            assert session.query(RawLead).count() == 0
            assert session.query(ScrapeRun).one().status == STATUS_TIMEOUT
        finally:
            session.close()

    def test_partial_output_is_preserved_under_logs(
        self, factory, monkeypatch, tmp_path
    ):
        _stub_timeout(monkeypatch, tmp_path, _jsonl(LEADS))

        run_scraper.execute_scrape_and_ingest(
            "Plumbing", "San Jose, CA", lat=37.3, lon=-121.9,
        )

        kept = list((tmp_path / "logs").glob("timeout_run*.json"))
        assert len(kept) == 1
        assert json.loads(kept[0].read_text().splitlines()[0])["title"] == "Acme Plumbing"

    def test_empty_results_file_still_records_timeout(
        self, factory, monkeypatch, tmp_path
    ):
        _stub_timeout(monkeypatch, tmp_path, "")

        with pytest.raises(subprocess.TimeoutExpired):
            run_scraper.execute_scrape_and_ingest(
                "Plumbing", "San Jose, CA", lat=37.3, lon=-121.9,
            )

        session = factory()
        try:
            assert session.query(ScrapeRun).one().status == STATUS_TIMEOUT
        finally:
            session.close()
        assert not (tmp_path / "logs").exists() or not list(
            (tmp_path / "logs").glob("timeout_run*.json")
        )


class TestPipelineSummaryFlagsPartialCoverage:
    """The closing banner must distinguish a partial sweep from a full one.

    Continue-on-salvage means a truncated run reaches `export_run_outputs`
    exactly like a complete one, so without this the operator's only cue that
    a city was half-swept would be a warning 30 minutes earlier in the log.
    """

    def test_reports_truncated_runs_and_suppresses_success_banner(
        self, factory, monkeypatch, tmp_path
    ):
        import run_pipeline

        monkeypatch.setattr(run_pipeline, "Session", factory)

        session = factory()
        session.add(ScrapeRun(query="Plumbing", location="San Jose, CA",
                              status="completed"))
        session.commit()
        marker = run_pipeline._latest_scrape_run_id()
        session.add(ScrapeRun(query="Plumbing", location="San Jose, CA",
                              status=STATUS_TIMEOUT))
        session.commit()
        session.close()

        truncated = run_pipeline._timed_out_runs_since(marker)

        assert len(truncated) == 1
        assert truncated[0][1:] == ("Plumbing", "San Jose, CA")

    def test_complete_runs_report_nothing(self, factory, monkeypatch, tmp_path):
        import run_pipeline

        monkeypatch.setattr(run_pipeline, "Session", factory)

        marker = run_pipeline._latest_scrape_run_id()
        session = factory()
        session.add(ScrapeRun(query="Plumbing", location="San Jose, CA",
                              status="completed"))
        session.commit()
        session.close()

        assert run_pipeline._timed_out_runs_since(marker) == []

    def test_summary_helpers_never_raise_on_a_broken_session(self, monkeypatch):
        """Diagnostics must not be able to fail a pipeline that worked."""
        import run_pipeline

        def exploding_session():
            raise RuntimeError("db gone")

        monkeypatch.setattr(run_pipeline, "Session", exploding_session)

        assert run_pipeline._latest_scrape_run_id() == 0
        assert run_pipeline._timed_out_runs_since(0) == []
