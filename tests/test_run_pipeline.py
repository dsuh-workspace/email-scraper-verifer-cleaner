"""Unit tests for run_pipeline and run_zip_batch orchestration."""

import importlib
import sys
import types

import pytest


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
        assert args.min_contacts == 500
        assert args.max_depth == 20
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
            verify=False, min_score=0,
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
            "scraper_concurrency": 3,
            "scraper_browser_pool_size": 2,
            "scraper_pages_per_browser": 1,
            "scraper_proxy_limit": 4,
            "scraper_disable_page_reuse": True,
        }

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

        contact_counts = iter([10, 60])
        monkeypatch.setattr(run_pipeline, "get_contact_count", lambda: next(contact_counts))

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

    def test_default_harvest_queries_plumbing(self, modules):
        run_pipeline, _ = modules
        assert run_pipeline._default_harvest_queries("Plumbing") == \
            run_pipeline.DEFAULT_HARVEST_QUERIES
        assert run_pipeline._default_harvest_queries("Drain cleaning") == \
            run_pipeline.DEFAULT_HARVEST_QUERIES

    def test_default_harvest_queries_unknown_falls_back_to_single(self, modules):
        run_pipeline, _ = modules
        # Unknown industry: no plumbing bias — just returns the base query.
        assert run_pipeline._default_harvest_queries("Roofing") == ("Roofing",)

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
