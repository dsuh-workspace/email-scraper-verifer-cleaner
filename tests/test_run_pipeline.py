"""Unit tests for run_pipeline and run_zip_batch orchestration."""

import importlib
import sys
import types

import pytest


class RecordingLogger:
    """Captures %-formatted log messages, one list per level.

    The module-scoped `modules` fixture installs a logger that swallows
    everything, so tests that assert on user-facing warnings monkeypatch this
    over `run_pipeline.logger` instead.
    """

    def __init__(self):
        self.messages: dict[str, list[str]] = {}

    def _log(self, level, args):
        msg, params = args[0], args[1:]
        self.messages.setdefault(level, []).append(msg % params if params else msg)

    def info(self, *args, **kwargs):
        self._log("info", args)

    def warning(self, *args, **kwargs):
        self._log("warning", args)

    def exception(self, *args, **kwargs):
        self._log("exception", args)

    def joined(self, level: str) -> str:
        return "\n".join(self.messages.get(level, []))


@pytest.fixture(scope="module")
def modules():
    fake_sqlalchemy = types.ModuleType("sqlalchemy")
    fake_sqlalchemy_orm = types.ModuleType("sqlalchemy.orm")
    fake_sqlalchemy_orm.sessionmaker = lambda *args, **kwargs: lambda: None

    fake_database = types.ModuleType("app.db.database")
    fake_database.engine = object()

    fake_create_tables = types.ModuleType("app.db.create_tables")
    fake_create_tables.Contact = type("Contact", (), {"id": object()})
    fake_create_tables.ExportHistory = type(
        "ExportHistory", (), {"contact_id": object(), "destination": object()}
    )
    fake_create_tables.EmailVerification = type(
        "EmailVerification", (), {"contact_id": object(), "score": object()}
    )
    fake_create_tables.init_db = lambda: None

    fake_verify = types.ModuleType("app.pipeline.verify_emails")
    fake_verify.verify_contacts_emails = lambda **_kw: None

    fake_logging = types.ModuleType("app.logging_config")

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            return None

    fake_logging.get_logger = lambda name: _Logger()
    fake_logging.setup_logging = lambda: None


    fake_export = types.ModuleType("app.pipeline.export_sheets")
    fake_export.export_new_leads = lambda **_kw: None

    fake_extract = types.ModuleType("app.pipeline.extract_emails")
    fake_extract.harvest_emails_from_websites = lambda: None

    fake_process = types.ModuleType("app.pipeline.process_leads")
    fake_process.process_and_deduplicate_leads = lambda: None

    fake_scraper = types.ModuleType("app.scraper.run_scraper")
    fake_scraper.execute_scrape_and_ingest = lambda *args, **kwargs: None
    fake_scraper.geocode_location = lambda location: (None, None, None)

    original_modules = {}
    for name, module in {
        "sqlalchemy": fake_sqlalchemy,
        "sqlalchemy.orm": fake_sqlalchemy_orm,
        "app.db.database": fake_database,
        "app.db.create_tables": fake_create_tables,
        "app.logging_config": fake_logging,
        "app.pipeline.export_sheets": fake_export,
        "app.pipeline.extract_emails": fake_extract,
        "app.pipeline.process_leads": fake_process,
        "app.pipeline.verify_emails": fake_verify,
        "app.scraper.run_scraper": fake_scraper,
    }.items():
        original_modules[name] = sys.modules.get(name)
        sys.modules[name] = module

    original_run_pipeline = sys.modules.pop("run_pipeline", None)
    original_run_zip_batch = sys.modules.pop("run_zip_batch", None)

    try:
        run_pipeline = importlib.import_module("run_pipeline")
        run_zip_batch = importlib.import_module("run_zip_batch")
        yield run_pipeline, run_zip_batch
    finally:
        for name, module in original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

        for name, module in {
            "run_pipeline": original_run_pipeline,
            "run_zip_batch": original_run_zip_batch,
        }.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class TestParseArgs:
    def test_required_args_parse(self, monkeypatch, modules):
        run_pipeline, _ = modules
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
        # Sentinel None = "flag not passed"; the pipeline substitutes the
        # defaults. Comparing against the literal 500/20 can't tell an
        # explicit --min-contacts 500 from an omitted flag.
        assert args.min_contacts is None
        assert args.max_depth is None
        assert run_pipeline.DEFAULT_MIN_CONTACTS == 500
        assert run_pipeline.DEFAULT_MAX_DEPTH == 20
        assert args.no_proxy is False
        assert args.no_scraper_proxy is False
        assert args.no_crawler_proxy is False

    def test_overrides_parse(self, monkeypatch, modules):
        run_pipeline, _ = modules
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

    def test_proxy_disable_flags_parse(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_pipeline.py",
                "--query",
                "HVAC",
                "--location",
                "Plano, TX",
                "--no-proxy",
                "--no-scraper-proxy",
                "--no-crawler-proxy",
            ],
        )

        args = run_pipeline.parse_args()

        assert args.no_proxy is True
        assert args.no_scraper_proxy is True
        assert args.no_crawler_proxy is True

    def test_missing_required_arg_exits(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_pipeline.py", "--query", "Plumbing"],
        )

        with pytest.raises(SystemExit):
            run_pipeline.parse_args()


class TestRunLocationPipeline:
    def test_stops_when_target_new_exportable_reached(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "geocode_location", lambda location: (1.0, 2.0, (0.5, 1.5, 1.5, 2.5)))
        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", lambda *args, **kwargs: None)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 12)

        counts = iter([5, 9])
        monkeypatch.setattr(
            run_pipeline,
            "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: next(counts),
        )

        metrics = run_pipeline.run_location_pipeline(
            query="Plumbing",
            location="95112, CA",
            max_depth=9,
            target_new_exportable=4,
            stale_iterations_limit=2,
        )

        assert metrics.depths_run == (1,)
        assert metrics.new_exportable_contacts == 4
        assert metrics.total_contacts == 12

    def test_stops_after_stale_iterations(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "geocode_location", lambda location: (1.0, 2.0, (0.5, 1.5, 1.5, 2.5)))
        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", lambda *args, **kwargs: None)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 12)

        counts = iter([5, 5, 5])
        monkeypatch.setattr(
            run_pipeline,
            "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: next(counts),
        )

        metrics = run_pipeline.run_location_pipeline(
            query="Plumbing",
            location="95112, CA",
            max_depth=9,
            target_new_exportable=4,
            stale_iterations_limit=2,
        )

        assert metrics.depths_run == (1, 3)
        assert metrics.new_exportable_contacts == 0
        assert metrics.stale_iterations == 2

    def test_stops_at_max_depth(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "geocode_location", lambda location: (1.0, 2.0, (0.5, 1.5, 1.5, 2.5)))
        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", lambda *args, **kwargs: None)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 12)

        counts = iter([5, 5, 5])
        monkeypatch.setattr(
            run_pipeline,
            "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: next(counts),
        )

        metrics = run_pipeline.run_location_pipeline(
            query="Plumbing",
            location="95112, CA",
            max_depth=3,
            target_new_exportable=4,
            stale_iterations_limit=None,
        )

        assert metrics.depths_run == (1, 3)
        assert metrics.final_depth == 3


class TestMain:
    def test_main_forwards_args(self, monkeypatch, modules):
        run_pipeline, _ = modules
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
                "--scraper-concurrency",
                "3",
                "--scraper-browser-pool-size",
                "2",
                "--scraper-pages-per-browser",
                "1",
                "--scraper-proxy-limit",
                "4",
                "--scraper-disable-page-reuse",
            ],
        )

        called = {}

        def fake_run_end_to_end_pipeline(
            query, location, min_contacts, max_depth,
            use_grid=False, cell_km=2.0, bbox=None,
            disable_scraper_proxy=False, disable_crawler_proxy=False,
            strategy="single-centroid", queries=None, zip_csv=None,
            verify=False, min_score=0, csv_path=None,
            scraper_concurrency=None,
            scraper_browser_pool_size=None,
            scraper_pages_per_browser=None,
            scraper_proxy_limit=None,
            scraper_disable_page_reuse=False,
        ):
            called["query"] = query
            called["location"] = location
            called["min_contacts"] = min_contacts
            called["max_depth"] = max_depth
            called["use_grid"] = use_grid
            called["cell_km"] = cell_km
            called["bbox"] = bbox
            called["disable_scraper_proxy"] = disable_scraper_proxy
            called["disable_crawler_proxy"] = disable_crawler_proxy
            called["strategy"] = strategy
            called["queries"] = queries
            called["zip_csv"] = zip_csv
            called["verify"] = verify
            called["min_score"] = min_score
            called["csv_path"] = csv_path
            called["scraper_concurrency"] = scraper_concurrency
            called["scraper_browser_pool_size"] = scraper_browser_pool_size
            called["scraper_pages_per_browser"] = scraper_pages_per_browser
            called["scraper_proxy_limit"] = scraper_proxy_limit
            called["scraper_disable_page_reuse"] = scraper_disable_page_reuse

        monkeypatch.setattr(run_pipeline, "run_end_to_end_pipeline", fake_run_end_to_end_pipeline)

        run_pipeline.main()

        assert called == {
            "query": "HVAC",
            "location": "Plano, TX",
            "min_contacts": 50,
            "max_depth": 9,
            "use_grid": False,
            "cell_km": 2.0,
            "bbox": None,
            "disable_scraper_proxy": False,
            "disable_crawler_proxy": False,
            "strategy": "single-centroid",
            "queries": None,
            "zip_csv": None,
            "verify": False,
            "min_score": 0,
            "csv_path": None,
            "scraper_concurrency": 3,
            "scraper_browser_pool_size": 2,
            "scraper_pages_per_browser": 1,
            "scraper_proxy_limit": 4,
            "scraper_disable_page_reuse": True,
        }

    def _capture_pipeline_kwargs(self, monkeypatch, run_pipeline) -> dict:
        called: dict = {}
        monkeypatch.setattr(
            run_pipeline,
            "run_end_to_end_pipeline",
            lambda **kwargs: called.update(kwargs),
        )
        return called

    def test_min_contacts_and_max_depth_passed_as_none_for_grid(
        self, monkeypatch, modules
    ):
        """Flags that grid ignores reach the pipeline as None, not as values."""
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Plumbing", "--location", "San Jose, CA",
             "--grid", "--min-contacts", "50", "--max-depth", "9"],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)
        recorder = RecordingLogger()
        monkeypatch.setattr(run_pipeline, "logger", recorder)

        run_pipeline.main()

        assert called["strategy"] == "grid"
        assert called["min_contacts"] is None
        assert called["max_depth"] is None
        warnings = recorder.joined("warning")
        assert "--min-contacts=50 supplied but strategy is grid" in warnings
        assert "--max-depth=9 supplied but strategy is grid" in warnings

    def test_no_flag_scope_warnings_for_single_centroid(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Plumbing", "--location", "San Jose, CA",
             "--min-contacts", "50", "--max-depth", "9"],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)
        recorder = RecordingLogger()
        monkeypatch.setattr(run_pipeline, "logger", recorder)

        run_pipeline.main()

        assert called["min_contacts"] == 50
        assert called["max_depth"] == 9
        assert "ignored" not in recorder.joined("warning")

    @pytest.mark.parametrize(
        "flag,value",
        [("--min-contacts", "0"), ("--min-contacts", "-5"),
         ("--max-depth", "0"), ("--max-depth", "-1")],
    )
    def test_non_positive_counts_rejected(self, monkeypatch, modules, flag, value):
        """#16: a 0/negative target or depth can't produce a sane loop."""
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Plumbing", "--location", "San Jose, CA",
             flag, value],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)

        with pytest.raises(SystemExit) as exc:
            run_pipeline.main()

        assert exc.value.code == 2
        assert called == {}

    def test_positive_counts_accepted(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Plumbing", "--location", "San Jose, CA",
             "--min-contacts", "1", "--max-depth", "1"],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)

        run_pipeline.main()

        assert called["min_contacts"] == 1
        assert called["max_depth"] == 1

    def test_queries_with_non_full_harvest_is_an_error(self, monkeypatch, modules):
        """Silently dropping --queries is how users trust a bad harvest."""
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Plumbing", "--location", "San Jose, CA",
             "--grid", "--queries", "Plumber,Leak repair"],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)

        with pytest.raises(SystemExit) as exc:
            run_pipeline.main()

        assert exc.value.code == 2
        assert called == {}

    def test_full_harvest_unknown_industry_without_queries_errors(
        self, monkeypatch, modules
    ):
        """Full-harvest's whole edge is Pass 2 — refuse to run it degraded."""
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Roofing", "--location", "San Jose, CA",
             "--strategy", "full-harvest"],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)

        with pytest.raises(SystemExit) as exc:
            run_pipeline.main()

        assert exc.value.code == 2
        assert called == {}

    def test_full_harvest_unknown_industry_with_queries_is_allowed(
        self, monkeypatch, modules
    ):
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Roofing", "--location", "San Jose, CA",
             "--strategy", "full-harvest",
             "--queries", "Roofing, Roof repair ,Roofer"],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)

        run_pipeline.main()

        assert called["queries"] == ("Roofing", "Roof repair", "Roofer")

    def test_queries_with_only_separators_errors(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(
            sys, "argv",
            ["run_pipeline.py", "--query", "Plumbing", "--location", "San Jose, CA",
             "--strategy", "full-harvest", "--queries", " , , "],
        )
        called = self._capture_pipeline_kwargs(monkeypatch, run_pipeline)

        with pytest.raises(SystemExit) as exc:
            run_pipeline.main()

        assert exc.value.code == 2
        assert called == {}

    def test_legacy_pipeline_keeps_increasing_depth_until_target(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "setup_logging", lambda: None)
        monkeypatch.setattr(run_pipeline, "init_db", lambda: None)
        monkeypatch.setattr(run_pipeline, "geocode_location", lambda location: (1.0, 2.0, (0.5, 1.5, 1.5, 2.5)))

        depths = []
        monkeypatch.setattr(
            run_pipeline,
            "execute_scrape_and_ingest",
            lambda query, location, lat=None, lon=None, depth=1, **_kw: depths.append(depth),
        )
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)

        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 60)
        # --min-contacts counts contacts THIS RUN made exportable, so the loop
        # stops on the baseline delta (30 → 80 = 50 new), not on the
        # cumulative DB total.
        exportable = iter([30, 60, 80])
        monkeypatch.setattr(
            run_pipeline,
            "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: next(exportable),
        )

        exported = []
        monkeypatch.setattr(run_pipeline, "export_new_leads", lambda **_kw: exported.append(True))

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="95112, CA",
            min_contacts=50,
            max_depth=5,
        )

        assert depths == [1, 3]
        assert exported == [True]

    def test_single_centroid_geocodes_once(self, monkeypatch, modules):
        """Delegating to run_location_pipeline must not double-hit Nominatim."""
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "setup_logging", lambda: None)
        monkeypatch.setattr(run_pipeline, "init_db", lambda: None)

        geocodes = []

        def fake_geocode(location):
            geocodes.append(location)
            return (1.0, 2.0, (0.5, 1.5, 1.5, 2.5))

        monkeypatch.setattr(run_pipeline, "geocode_location", fake_geocode)
        seen = []
        monkeypatch.setattr(
            run_pipeline,
            "execute_scrape_and_ingest",
            lambda query, location, lat=None, lon=None, depth=1, **_kw:
                seen.append((lat, lon)),
        )
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 5)
        monkeypatch.setattr(
            run_pipeline, "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: 5,
        )
        monkeypatch.setattr(run_pipeline, "export_new_leads", lambda **_kw: None)

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="95112, CA",
            min_contacts=1,
            max_depth=1,
        )

        assert geocodes == ["95112, CA"]
        assert seen == [(1.0, 2.0)]

    def test_grid_mode_single_pass_uses_bbox(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "setup_logging", lambda: None)
        monkeypatch.setattr(run_pipeline, "init_db", lambda: None)
        # Nominatim bbox flows through when no override.
        monkeypatch.setattr(
            run_pipeline,
            "geocode_location",
            lambda location: (37.3, -121.8, (37.21, -122.05, 37.47, -121.75)),
        )

        calls = []

        def fake_scrape(query, location, lat=None, lon=None, depth=1, bbox=None, cell_km=None, **_kw):
            calls.append({"bbox": bbox, "cell_km": cell_km, "depth": depth, "lat": lat, "lon": lon})

        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", fake_scrape)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 0)
        # Grid reports a new-exportable delta, so it reads this twice.
        monkeypatch.setattr(
            run_pipeline, "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: 0,
        )
        monkeypatch.setattr(run_pipeline, "export_new_leads", lambda **_kw: None)

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="San Jose, CA",
            min_contacts=999,
            max_depth=20,
            use_grid=True,
            cell_km=2.0,
        )

        # Exactly one scrape (grid mode does not loop on depth)
        assert len(calls) == 1
        assert calls[0]["bbox"] == (37.21, -122.05, 37.47, -121.75)
        assert calls[0]["cell_km"] == 2.0

    def test_grid_mode_respects_explicit_bbox(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "setup_logging", lambda: None)
        monkeypatch.setattr(run_pipeline, "init_db", lambda: None)
        # Nominatim returns a different bbox; explicit override should win.
        monkeypatch.setattr(
            run_pipeline,
            "geocode_location",
            lambda location: (0.0, 0.0, (10.0, 10.0, 20.0, 20.0)),
        )

        received = {}

        def fake_scrape(query, location, lat=None, lon=None, depth=1, bbox=None, cell_km=None, **_kw):
            received["bbox"] = bbox
            received["cell_km"] = cell_km

        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", fake_scrape)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 0)
        monkeypatch.setattr(
            run_pipeline, "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: 0,
        )
        monkeypatch.setattr(run_pipeline, "export_new_leads", lambda **_kw: None)

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="anywhere",
            min_contacts=1,
            max_depth=20,
            use_grid=True,
            cell_km=1.5,
            bbox=(1.0, 1.0, 2.0, 2.0),
        )

        assert received["bbox"] == (1.0, 1.0, 2.0, 2.0)
        assert received["cell_km"] == 1.5

    def test_grid_mode_errors_without_bbox(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "setup_logging", lambda: None)
        monkeypatch.setattr(run_pipeline, "init_db", lambda: None)
        # Nominatim returns no bbox.
        monkeypatch.setattr(
            run_pipeline,
            "geocode_location",
            lambda location: (1.0, 2.0, None),
        )
        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", lambda *a, **kw: None)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **kwargs: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 0)
        monkeypatch.setattr(run_pipeline, "export_new_leads", lambda **_kw: None)

        import pytest
        with pytest.raises(SystemExit):
            # RuntimeError is caught + logged inside run_end_to_end_pipeline, then sys.exit(1)
            run_pipeline.run_end_to_end_pipeline(
                query="Plumbing",
                location="unmappable",
                use_grid=True,
            )

    def test_parse_bbox_rejects_wrong_arity(self, modules):
        run_pipeline, _ = modules
        import pytest
        with pytest.raises(ValueError):
            run_pipeline._parse_bbox("1.0,2.0,3.0")

    def test_parse_bbox_rejects_reversed(self, modules):
        run_pipeline, _ = modules
        import pytest
        with pytest.raises(ValueError):
            # min_lat > max_lat
            run_pipeline._parse_bbox("5.0,1.0,3.0,2.0")

    def test_parse_bbox_happy(self, modules):
        run_pipeline, _ = modules
        assert run_pipeline._parse_bbox("37.21,-122.05,37.47,-121.75") == (
            37.21, -122.05, 37.47, -121.75,
        )


class TestFullHarvestStrategy:
    """Full-harvest = grid pass + multi-query slow pass + optional fast ZIP pass."""

    def _wire(self, monkeypatch, run_pipeline, calls):
        monkeypatch.setattr(run_pipeline, "setup_logging", lambda: None)
        monkeypatch.setattr(run_pipeline, "init_db", lambda: None)
        monkeypatch.setattr(
            run_pipeline, "geocode_location",
            lambda location: (37.3, -121.8, (37.21, -122.05, 37.47, -121.75)),
        )

        def fake_scrape(query, location, lat=None, lon=None, depth=1,
                        bbox=None, cell_km=None, disable_proxy=False,
                        queries=None, fast_mode=None, lang="en",
                        concurrency=None, browser_pool_size=None,
                        pages_per_browser=None, proxy_limit=None,
                        disable_page_reuse=False):
            calls.append({
                "query": query, "location": location,
                "lat": lat, "lon": lon, "depth": depth,
                "bbox": bbox, "cell_km": cell_km,
                "queries": tuple(queries) if queries else None,
                "fast_mode": fast_mode,
                "concurrency": concurrency,
                "browser_pool_size": browser_pool_size,
                "pages_per_browser": pages_per_browser,
                "proxy_limit": proxy_limit,
                "disable_page_reuse": disable_page_reuse,
            })

        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", fake_scrape)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites",
                            lambda **_: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 0)
        # Full-harvest reports a new-exportable delta like the depth loop does,
        # so it takes a baseline before Pass 1 and reads the count again at the
        # end — both hit the DB, which is stubbed out here.
        monkeypatch.setattr(
            run_pipeline, "get_exportable_contact_count",
            lambda destination=run_pipeline.LEGACY_EXPORT_DESTINATION: 0,
        )
        monkeypatch.setattr(run_pipeline, "export_new_leads", lambda **_kw: None)

    def test_full_harvest_runs_grid_then_multi_query(self, monkeypatch, modules):
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="San Jose, CA",
            strategy="full-harvest",
            cell_km=2.0,
        )

        # No zip-csv → 2 passes: grid, then multi-query.
        assert len(calls) == 2

        # PASS 1: grid single query
        assert calls[0]["bbox"] == (37.21, -122.05, 37.47, -121.75)
        assert calls[0]["cell_km"] == 2.0
        assert calls[0]["queries"] is None
        assert calls[0]["depth"] == 3

        # PASS 2: multi-query slow at centroid
        assert calls[1]["bbox"] is None
        assert calls[1]["lat"] == 37.3
        assert calls[1]["lon"] == -121.8
        assert calls[1]["depth"] == 10
        assert calls[1]["fast_mode"] is False
        assert calls[1]["queries"] == tuple(run_pipeline.DEFAULT_HARVEST_QUERIES)

    def test_full_harvest_forwards_scraper_tuning(self, monkeypatch, modules):
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="San Jose, CA",
            strategy="full-harvest",
            scraper_concurrency=3,
            scraper_browser_pool_size=2,
            scraper_pages_per_browser=1,
            scraper_proxy_limit=4,
        )

        assert len(calls) == 2
        assert calls[0]["concurrency"] == 3
        assert calls[0]["browser_pool_size"] == 2
        assert calls[0]["pages_per_browser"] == 1
        assert calls[0]["proxy_limit"] == 4
        assert calls[1]["concurrency"] == 3
        assert calls[1]["browser_pool_size"] == 2
        assert calls[1]["pages_per_browser"] == 1
        assert calls[1]["proxy_limit"] == 4

    def test_full_harvest_uses_custom_queries(self, monkeypatch, modules):
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="San Jose, CA",
            strategy="full-harvest",
            queries=("Plumbing", "Plumber", "Leak repair"),
        )
        assert calls[1]["queries"] == ("Plumbing", "Plumber", "Leak repair")

    def test_full_harvest_zip_pass_optional(self, monkeypatch, tmp_path, modules):
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)

        zip_csv = tmp_path / "zips.csv"
        zip_csv.write_text(
            "zip,city,state\n95112,San Jose,CA\n95123,San Jose,CA\n",
            encoding="utf-8",
        )

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="San Jose, CA",
            strategy="full-harvest",
            zip_csv=str(zip_csv),
        )

        # 4 passes: grid, multi-query, 2×zip
        assert len(calls) == 4
        # Last two are fast-mode single-centroid at ZIP centroids.
        assert calls[2]["fast_mode"] is True
        assert calls[2]["bbox"] is None
        assert calls[3]["fast_mode"] is True
        assert calls[3]["bbox"] is None

    def test_full_harvest_errors_without_bbox(self, monkeypatch, modules):
        run_pipeline, _ = modules
        monkeypatch.setattr(run_pipeline, "setup_logging", lambda: None)
        monkeypatch.setattr(run_pipeline, "init_db", lambda: None)
        # Nominatim returns no bbox.
        monkeypatch.setattr(
            run_pipeline, "geocode_location",
            lambda location: (37.3, -121.8, None),
        )
        monkeypatch.setattr(run_pipeline, "execute_scrape_and_ingest", lambda *a, **kw: None)
        monkeypatch.setattr(run_pipeline, "process_and_deduplicate_leads", lambda: None)
        monkeypatch.setattr(run_pipeline, "harvest_emails_from_websites", lambda **_: None)
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: 0)
        monkeypatch.setattr(run_pipeline, "export_new_leads", lambda **_kw: None)

        import pytest
        with pytest.raises(SystemExit):
            run_pipeline.run_end_to_end_pipeline(
                query="Plumbing",
                location="unmappable",
                strategy="full-harvest",
            )

    def test_resolve_strategy_from_flags(self, monkeypatch, modules):
        run_pipeline, _ = modules
        import argparse
        ns = argparse.Namespace(strategy=None, grid=True)
        assert run_pipeline._resolve_strategy(ns) == "grid"
        ns2 = argparse.Namespace(strategy=None, grid=False)
        assert run_pipeline._resolve_strategy(ns2) == "single-centroid"
        ns3 = argparse.Namespace(strategy="full-harvest", grid=False)
        assert run_pipeline._resolve_strategy(ns3) == "full-harvest"

    def test_default_harvest_queries_hvac(self, modules):
        run_pipeline, _ = modules
        assert run_pipeline._default_harvest_queries("HVAC") == \
            run_pipeline.DEFAULT_HVAC_HARVEST_QUERIES
        assert run_pipeline._default_harvest_queries("Heating and cooling") == \
            run_pipeline.DEFAULT_HVAC_HARVEST_QUERIES

    @pytest.mark.parametrize(
        "query",
        ["AC repair", "A/C installation", "Boiler service", "Refrigeration",
         "Mini split install", "Ductwork", "ac repair"],
    )
    def test_default_harvest_queries_hvac_shorthand(self, modules, query):
        """HVAC trade shorthand must not fall through to the unknown branch."""
        run_pipeline, _ = modules
        assert run_pipeline._default_harvest_queries(query) == \
            run_pipeline.DEFAULT_HVAC_HARVEST_QUERIES

    @pytest.mark.parametrize("query", ["Backflow testing", "Vacuum truck service"])
    def test_bare_ac_is_word_bounded(self, modules, query):
        """'ac' inside another word must not classify as HVAC."""
        run_pipeline, _ = modules
        assert run_pipeline._default_harvest_queries(query) != \
            run_pipeline.DEFAULT_HVAC_HARVEST_QUERIES

    def test_default_harvest_queries_plumbing(self, modules):
        run_pipeline, _ = modules
        assert run_pipeline._default_harvest_queries("Plumbing") == \
            run_pipeline.DEFAULT_HARVEST_QUERIES
        assert run_pipeline._default_harvest_queries("Drain cleaning") == \
            run_pipeline.DEFAULT_HARVEST_QUERIES

    def test_leak_alone_is_not_plumbing(self, modules):
        """'leak' spans both trades, so it can't decide an industry alone."""
        run_pipeline, _ = modules
        assert run_pipeline._default_harvest_queries("Leak detection") is None
        # ...and an HVAC-qualified leak query classifies as HVAC.
        assert run_pipeline._default_harvest_queries("AC leak repair") == \
            run_pipeline.DEFAULT_HVAC_HARVEST_QUERIES

    def test_query_naming_both_trades_is_ambiguous(self, modules):
        """One vertical per run, so a both-trades query resolves to neither.

        Guards against the first-match-wins chain this replaced, where
        whichever branch was checked first silently shadowed the other.
        """
        run_pipeline, _ = modules
        assert run_pipeline._default_harvest_queries("Plumbing and HVAC") is None
        assert run_pipeline._default_harvest_queries("HVAC and drain cleaning") is None

    def test_default_harvest_queries_unknown_returns_none(self, modules):
        run_pipeline, _ = modules
        # Unknown industry: no plumbing bias, and no logging side effect —
        # the caller decides how loud to be.
        assert run_pipeline._default_harvest_queries("Roofing") is None

    def test_default_harvest_queries_rejects_blank_query(self, modules):
        run_pipeline, _ = modules
        for blank in ("", "   ", None):
            with pytest.raises(ValueError):
                run_pipeline._default_harvest_queries(blank)

    def test_pass2_degradation_warns_and_uses_base_query(self, monkeypatch, modules):
        """Unknown industry: Pass 2 degrades, but says so — twice."""
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)
        recorder = RecordingLogger()
        monkeypatch.setattr(run_pipeline, "logger", recorder)

        run_pipeline.run_end_to_end_pipeline(
            query="Roofing",
            location="San Jose, CA",
            strategy="full-harvest",
        )

        assert calls[1]["queries"] == ("Roofing",)
        assert "No built-in harvest query set for 'Roofing'" in \
            recorder.joined("warning")
        # The completion line must carry the caveat too — that's the line
        # operators read when they tail cron output.
        assert "PASS 2 ran degraded" in recorder.joined("info")

    def test_no_degradation_note_when_pass2_ran_full(self, monkeypatch, modules):
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)
        recorder = RecordingLogger()
        monkeypatch.setattr(run_pipeline, "logger", recorder)

        run_pipeline.run_end_to_end_pipeline(
            query="Plumbing",
            location="San Jose, CA",
            strategy="full-harvest",
        )

        assert "degraded" not in recorder.joined("info")
        assert "No built-in harvest query set" not in recorder.joined("warning")

    def test_pass2_skipped_does_not_warn_about_queries(self, monkeypatch, modules):
        """No centroid → no Pass 2 → no warning about Pass 2's query set."""
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)
        # bbox present (Pass 1 can run) but no centroid for Pass 2.
        monkeypatch.setattr(
            run_pipeline, "geocode_location",
            lambda location: (None, None, (37.21, -122.05, 37.47, -121.75)),
        )
        recorder = RecordingLogger()
        monkeypatch.setattr(run_pipeline, "logger", recorder)

        run_pipeline.run_end_to_end_pipeline(
            query="Roofing",
            location="San Jose, CA",
            strategy="full-harvest",
        )

        warnings = recorder.joined("warning")
        assert "Skipping PASS 2" in warnings
        assert "No built-in harvest query set" not in warnings
        assert "PASS 2 ran degraded" not in recorder.joined("info")

    def test_empty_queries_tuple_is_rejected(self, monkeypatch, modules):
        """queries=() is a caller bug, not a request for the defaults."""
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)

        # run_end_to_end_pipeline wraps failures in sys.exit(1).
        with pytest.raises(SystemExit):
            run_pipeline.run_end_to_end_pipeline(
                query="Plumbing",
                location="San Jose, CA",
                strategy="full-harvest",
                queries=(),
            )
        assert calls == []

    def test_full_harvest_uses_hvac_defaults_when_query_is_hvac(
        self, monkeypatch, modules
    ):
        run_pipeline, _ = modules
        calls: list[dict] = []
        self._wire(monkeypatch, run_pipeline, calls)

        run_pipeline.run_end_to_end_pipeline(
            query="HVAC",
            location="Plano, TX",
            strategy="full-harvest",
        )
        # Pass 2 = multi-query. Should be HVAC set, not plumbing.
        assert calls[1]["queries"] == run_pipeline.DEFAULT_HVAC_HARVEST_QUERIES


class TestZipBatchHelpers:
    def test_row_location_prefers_explicit_location(self, modules):
        _, run_zip_batch = modules
        assert run_zip_batch._row_location(
            {
                "location": "San Jose, CA 95112",
                "zip": "95112",
                "city": "San Jose",
                "state": "CA",
            }
        ) == "San Jose, CA 95112"

    def test_row_location_builds_from_zip_city_state(self, modules):
        _, run_zip_batch = modules
        assert run_zip_batch._row_location(
            {"zip": "95112", "city": "San Jose", "state": "CA"}
        ) == "San Jose, CA 95112"

    def test_load_locations_skips_bad_rows(self, tmp_path, modules):
        _, run_zip_batch = modules
        path = tmp_path / "zips.csv"
        path.write_text("zip,city,state\n95112,San Jose,CA\n,,\n95123,San Jose,CA\n", encoding="utf-8")

        assert run_zip_batch.load_locations(str(path)) == [
            "San Jose, CA 95112",
            "San Jose, CA 95123",
        ]

    def test_load_locations_rejects_empty_result(self, tmp_path, modules):
        _, run_zip_batch = modules
        path = tmp_path / "zips.csv"
        path.write_text("zip,city,state\n,,\n", encoding="utf-8")

        with pytest.raises(ValueError, match="no usable rows"):
            run_zip_batch.load_locations(str(path))


class TestZipBatchMain:
    def test_main_runs_each_location_and_exports_once(self, monkeypatch, tmp_path, modules):
        run_pipeline, run_zip_batch = modules
        path = tmp_path / "zips.csv"
        path.write_text("zip\n95112\n95123\n", encoding="utf-8")

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_zip_batch.py",
                "--query",
                "Plumbing",
                "--zip-file",
                str(path),
                "--target-new-exportable",
                "7",
            ],
        )

        calls = []
        monkeypatch.setattr(run_zip_batch, "setup_logging", lambda: None)
        monkeypatch.setattr(
            run_zip_batch,
            "run_location_pipeline",
            lambda **kwargs: calls.append(kwargs) or run_pipeline.LocationRunMetrics(
                depths_run=[1],
                final_depth=1,
                total_contacts=10,
                exportable_contacts=10,
                baseline_exportable_contacts=3,
                new_exportable_contacts=7,
                stale_iterations=0,
            ),
        )

        init_db_calls = []
        monkeypatch.setattr(run_zip_batch, "init_db", lambda: init_db_calls.append(True))

        exported = []
        monkeypatch.setattr(run_zip_batch, "export_new_leads", lambda **_kw: exported.append(True))

        run_zip_batch.main()

        assert init_db_calls == [True]
        assert [call["location"] for call in calls] == ["95112", "95123"]
        assert all(call["target_new_exportable"] == 7 for call in calls)
        assert exported == [True]

    def test_continues_after_location_failure(self, monkeypatch, tmp_path, modules):
        run_pipeline, run_zip_batch = modules

        path = tmp_path / "zips.csv"
        path.write_text("zip\n95112\n95123\n", encoding="utf-8")

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_zip_batch.py",
                "--query",
                "Plumbing",
                "--zip-file",
                str(path),
            ],
        )

        seen = []

        def fake_run_location_pipeline(**kwargs):
            seen.append(kwargs["location"])
            if kwargs["location"] == "95112":
                raise RuntimeError("boom")
            return run_pipeline.LocationRunMetrics(
                depths_run=(1,),
                final_depth=1,
                total_contacts=10,
                exportable_contacts=10,
                baseline_exportable_contacts=3,
                new_exportable_contacts=7,
                stale_iterations=0,
            )

        monkeypatch.setattr(run_zip_batch, "run_location_pipeline", fake_run_location_pipeline)

        exported = []
        monkeypatch.setattr(run_zip_batch, "export_new_leads", lambda **_kw: exported.append(True))

        run_zip_batch.main()

        assert seen == ["95112", "95123"]
        assert exported == [True]


class TestZipBatchProxyFlags:
    def test_zip_batch_forwards_proxy_disable_flags(self, monkeypatch, modules):
        _, run_zip_batch = modules
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_zip_batch.py",
                "--query",
                "Plumbing",
                "--zip-file",
                "zips.csv",
                "--no-proxy",
            ],
        )
        monkeypatch.setattr(run_zip_batch, "setup_logging", lambda: None)
        monkeypatch.setattr(run_zip_batch, "init_db", lambda: None)
        monkeypatch.setattr(run_zip_batch, "load_locations", lambda path: ["95112"])
        monkeypatch.setattr(run_zip_batch, "export_new_leads", lambda **_kw: None)

        called = {}

        def fake_run_location_pipeline(**kwargs):
            called.update(kwargs)
            return types.SimpleNamespace(new_exportable_contacts=0, depths_run=(), total_contacts=0)

        monkeypatch.setattr(run_zip_batch, "run_location_pipeline", fake_run_location_pipeline)

        run_zip_batch.main()

        assert called["disable_scraper_proxy"] is True
        assert called["disable_crawler_proxy"] is True

    def test_zip_batch_forwards_scraper_tuning_flags(self, monkeypatch, modules):
        _, run_zip_batch = modules
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_zip_batch.py",
                "--query",
                "Plumbing",
                "--zip-file",
                "zips.csv",
                "--scraper-concurrency",
                "3",
                "--scraper-browser-pool-size",
                "2",
                "--scraper-pages-per-browser",
                "1",
                "--scraper-proxy-limit",
                "4",
                "--scraper-disable-page-reuse",
            ],
        )
        monkeypatch.setattr(run_zip_batch, "setup_logging", lambda: None)
        monkeypatch.setattr(run_zip_batch, "init_db", lambda: None)
        monkeypatch.setattr(run_zip_batch, "load_locations", lambda path: ["95112"])
        monkeypatch.setattr(run_zip_batch, "export_new_leads", lambda **_kw: None)

        called = {}

        def fake_run_location_pipeline(**kwargs):
            called.update(kwargs)
            return types.SimpleNamespace(new_exportable_contacts=0, depths_run=(), total_contacts=0)

        monkeypatch.setattr(run_zip_batch, "run_location_pipeline", fake_run_location_pipeline)

        run_zip_batch.main()

        assert called["scraper_concurrency"] == 3
        assert called["scraper_browser_pool_size"] == 2
        assert called["scraper_pages_per_browser"] == 1
        assert called["scraper_proxy_limit"] == 4
        assert called["scraper_disable_page_reuse"] is True


class TestZipBatchStrategies:
    """#R6 — run_zip_batch dispatches all three strategies, not just the loop."""

    def _zip_file(self, tmp_path, rows="zip\n95112\n95123\n"):
        path = tmp_path / "zips.csv"
        path.write_text(rows, encoding="utf-8")
        return str(path)

    def _wire(self, monkeypatch, run_pipeline, run_zip_batch, *, bbox=(1.0, 1.0, 2.0, 2.0)):
        """Stub out logging, DB, geocode, and the three strategy entrypoints.

        Returns a dict of per-strategy call lists so a test can assert which
        one ran and with what geo.
        """
        monkeypatch.setattr(run_zip_batch, "setup_logging", lambda: None)
        monkeypatch.setattr(run_zip_batch, "init_db", lambda: None)
        monkeypatch.setattr(run_zip_batch, "export_new_leads", lambda **_kw: None)
        monkeypatch.setattr(
            run_zip_batch, "geocode_location",
            lambda location: (37.3, -121.8, bbox),
        )

        calls = {"single": [], "grid": [], "harvest": []}

        def recorder(bucket):
            def fake(**kwargs):
                calls[bucket].append(kwargs)
                return run_pipeline.LocationRunMetrics(
                    depths_run=(3,),
                    final_depth=3,
                    total_contacts=1,
                    exportable_contacts=1,
                    baseline_exportable_contacts=0,
                    new_exportable_contacts=1,
                    stale_iterations=0,
                )
            return fake

        monkeypatch.setattr(run_zip_batch, "run_location_pipeline", recorder("single"))
        monkeypatch.setattr(run_zip_batch, "run_location_grid", recorder("grid"))
        monkeypatch.setattr(run_zip_batch, "run_location_full_harvest", recorder("harvest"))
        return calls

    def test_defaults_to_single_centroid(self, monkeypatch, tmp_path, modules):
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path),
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        run_zip_batch.main()

        assert len(calls["single"]) == 2
        assert not calls["grid"] and not calls["harvest"]

    def test_grid_strategy_passes_per_row_bbox(self, monkeypatch, tmp_path, modules):
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path),
            "--strategy", "grid", "--cell-km", "1.5",
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        run_zip_batch.main()

        assert len(calls["grid"]) == 2
        assert not calls["single"]
        assert all(c["bbox"] == (1.0, 1.0, 2.0, 2.0) for c in calls["grid"])
        assert all(c["cell_km"] == 1.5 for c in calls["grid"])

    def test_grid_flag_is_shorthand_for_strategy(self, monkeypatch, tmp_path, modules):
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path), "--grid",
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        run_zip_batch.main()

        assert len(calls["grid"]) == 2

    def test_full_harvest_passes_centroid_and_skips_pass3(
        self, monkeypatch, tmp_path, modules,
    ):
        """The batch IS the ZIP sweep, so Pass 3 must never be handed a CSV."""
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path), "--strategy", "full-harvest",
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        run_zip_batch.main()

        assert len(calls["harvest"]) == 2
        for call in calls["harvest"]:
            # Two-step contract: the CLI only *validates* that a variant set is
            # derivable and forwards None; run_location_full_harvest does the
            # deriving. Sending a pre-derived tuple would work but would put
            # the industry defaults in two places.
            assert call["queries"] is None
            assert call["lat"] == 37.3 and call["lon"] == -121.8
            assert "zip_csv" not in call

    def test_full_harvest_honors_explicit_queries(self, monkeypatch, tmp_path, modules):
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Roofing",
            "--zip-file", self._zip_file(tmp_path), "--strategy", "full-harvest",
            "--queries", "Roofer,Roof repair",
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        run_zip_batch.main()

        assert all(c["queries"] == ("Roofer", "Roof repair") for c in calls["harvest"])

    def test_full_harvest_rejects_underivable_query(self, monkeypatch, tmp_path, modules):
        """Same exit-2 contract as run_pipeline: no variant set, no run."""
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Roofing",
            "--zip-file", self._zip_file(tmp_path), "--strategy", "full-harvest",
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        with pytest.raises(SystemExit) as excinfo:
            run_zip_batch.main()

        assert excinfo.value.code == 2
        assert not calls["harvest"]

    def test_queries_with_non_full_harvest_is_an_error(self, monkeypatch, tmp_path, modules):
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path), "--strategy", "grid",
            "--queries", "a,b",
        ])
        self._wire(monkeypatch, run_pipeline, run_zip_batch)

        with pytest.raises(SystemExit) as excinfo:
            run_zip_batch.main()

        assert excinfo.value.code == 2

    def test_unmappable_row_does_not_end_the_batch(self, monkeypatch, tmp_path, modules):
        """Grid raises on a bbox-less row; the batch must keep going."""
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path), "--strategy", "grid",
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        exported = []
        monkeypatch.setattr(run_zip_batch, "export_new_leads",
                            lambda **_kw: exported.append(True))

        seen = []

        def boom_then_ok(**kwargs):
            seen.append(kwargs["location"])
            if len(seen) == 1:
                raise RuntimeError("Grid mode requires a bounding box.")
            return run_pipeline.LocationRunMetrics(
                depths_run=(3,), final_depth=3, total_contacts=1,
                exportable_contacts=1, baseline_exportable_contacts=0,
                new_exportable_contacts=1, stale_iterations=0,
            )

        monkeypatch.setattr(run_zip_batch, "run_location_grid", boom_then_ok)

        run_zip_batch.main()

        assert seen == ["95112", "95123"]
        assert exported == [True]
        assert calls["single"] == []

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("--target-new-exportable", "0"),
            ("--max-depth", "0"),
            ("--stale-iterations", "-1"),
            ("--cell-km", "0"),
        ],
    )
    def test_non_positive_bounds_exit_2(self, monkeypatch, tmp_path, modules, flag, value):
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path), flag, value,
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        with pytest.raises(SystemExit) as excinfo:
            run_zip_batch.main()

        assert excinfo.value.code == 2
        assert not any(calls.values())

    def test_depth_flags_warn_when_strategy_ignores_them(
        self, monkeypatch, tmp_path, modules,
    ):
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path), "--strategy", "grid",
            "--max-depth", "8",
        ])
        self._wire(monkeypatch, run_pipeline, run_zip_batch)
        recorder = RecordingLogger()
        monkeypatch.setattr(run_zip_batch, "logger", recorder)

        run_zip_batch.main()

        warnings = recorder.joined("warning")
        assert "--max-depth=8" in warnings
        assert "does not loop on depth" in warnings

    def test_no_depth_warning_when_flags_untouched(self, monkeypatch, tmp_path, modules):
        """Defaults are None, so an unpassed flag can't look like a passed one."""
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path), "--strategy", "grid",
        ])
        self._wire(monkeypatch, run_pipeline, run_zip_batch)
        recorder = RecordingLogger()
        monkeypatch.setattr(run_zip_batch, "logger", recorder)

        run_zip_batch.main()

        assert "ignored" not in recorder.joined("warning")

    def test_single_centroid_resolves_none_defaults(self, monkeypatch, tmp_path, modules):
        """None means "not passed", so the pipeline must still get real numbers."""
        run_pipeline, run_zip_batch = modules
        monkeypatch.setattr(sys, "argv", [
            "run_zip_batch.py", "--query", "Plumbing",
            "--zip-file", self._zip_file(tmp_path, "zip\n95112\n"),
        ])
        calls = self._wire(monkeypatch, run_pipeline, run_zip_batch)

        run_zip_batch.main()

        call = calls["single"][0]
        assert call["target_new_exportable"] == run_zip_batch.DEFAULT_TARGET_NEW_EXPORTABLE
        assert call["max_depth"] == run_zip_batch.DEFAULT_MAX_DEPTH
        assert call["stale_iterations_limit"] == run_zip_batch.DEFAULT_STALE_ITERATIONS
