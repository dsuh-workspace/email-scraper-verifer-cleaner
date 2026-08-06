"""
Unit tests for app.pipeline.extract_emails helpers.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.pipeline.extract_emails as extract_module
from app.pipeline.extract_emails import (
    DEFAULT_CRAWL_MAX_ATTEMPTS,
    DEFAULT_CRAWL_RETRY_AFTER_HOURS,
    _build_crawler_proxies,
    _crawl_retry_policy,
    _is_within_cooldown,
    _record_crawl_attempt,
    extract_emails_from_html,
)


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

    def test_retina_asset_filenames_excluded(self):
        # "logo@2x.avif" survives the regex because "@2x" reads as a
        # local-part/domain split. Real San Jose exports picked up nine of
        # these before .avif/.ico were added to EXCLUDE_EXTENSIONS.
        html = (
            "<img src=\"logo@2x.avif\"><link rel=icon href=\"favicon@2x.ico\">"
            " contact: real@acme.com"
        )
        emails = extract_emails_from_html(html)
        assert emails == ["real@acme.com"]

    def test_sentry_dsn_excluded(self):
        html = (
            'Sentry.init({dsn: "abc123@o12345.ingest.sentry.io"});'
            ' contact: real@acme.com'
        )
        emails = extract_emails_from_html(html)
        assert "real@acme.com" in emails
        assert not any("sentry.io" in e for e in emails)

    def test_font_designer_localpart_excluded(self):
        """Webfont license headers leak the designer's freemail address.

        EXCLUDE_DOMAINS cannot filter this one: it is @gmail.com, and blocking
        gmail would drop most owner-operator contractors. The 2026-08-04 Santa
        Clara HVAC export shipped impallari@gmail.com as a lead for a business
        whose only connection was embedding one of his fonts.
        """
        html = (
            "/* Copyright (c) Pablo Impallari (www.impallari.com|"
            "impallari@gmail.com) */ contact: real@acme.com"
        )
        emails = extract_emails_from_html(html)
        assert emails == ["real@acme.com"]

    def test_all_x_placeholder_excluded(self):
        html = "email: xxx@xxx.xxx -- contact: real@acme.com"
        emails = extract_emails_from_html(html)
        assert emails == ["real@acme.com"]

    def test_theme_boilerplate_address_domain_excluded(self):
        # "email@address.com" is theme filler. The existing "email.com" entry
        # does not catch it -- substring matching stops at the "@".
        html = "email@address.com then real@acme.com"
        emails = extract_emails_from_html(html)
        assert emails == ["real@acme.com"]

    def test_empty_html(self):
        assert extract_emails_from_html("") == []

    def test_no_emails_present(self):
        assert extract_emails_from_html("<html>no contact info</html>") == []

    def test_return_sorted(self):
        html = "zeta@acme.com alpha@acme.com beta@acme.com"
        assert extract_emails_from_html(html) == [
            "alpha@acme.com", "beta@acme.com", "zeta@acme.com",
        ]


@pytest.fixture(autouse=True)
def _clear_crawler_proxy_env(monkeypatch):
    monkeypatch.delenv("CRAWLER_PROXY", raising=False)
    monkeypatch.delenv("CRAWLER_HTTP_PROXY", raising=False)
    monkeypatch.delenv("CRAWLER_HTTPS_PROXY", raising=False)
    monkeypatch.delenv("CRAWLER_PROXY_FILE", raising=False)


class TestBuildCrawlerProxies:
    def test_returns_none_when_unset(self):
        assert _build_crawler_proxies() is None

    def test_uses_fallback_for_both_schemes(self, monkeypatch):
        monkeypatch.setenv("CRAWLER_PROXY", "http://proxy.example.com:8080")

        assert _build_crawler_proxies() == {
            "http": "http://proxy.example.com:8080",
            "https": "http://proxy.example.com:8080",
        }

    def test_prefers_split_values(self, monkeypatch):
        monkeypatch.setenv("CRAWLER_PROXY", "http://fallback.example.com:8080")
        monkeypatch.setenv("CRAWLER_HTTP_PROXY", "http://http.example.com:8081")
        monkeypatch.setenv("CRAWLER_HTTPS_PROXY", "https://https.example.com:8443")

        assert _build_crawler_proxies() == {
            "http": "http://http.example.com:8081",
            "https": "https://https.example.com:8443",
        }

    def test_rejects_invalid_scheme(self, monkeypatch):
        monkeypatch.setenv("CRAWLER_PROXY", "ftp://proxy.example.com:21")
        monkeypatch.delenv("CRAWLER_HTTP_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_HTTPS_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_PROXY_FILE", raising=False)

        with pytest.raises(ValueError, match="Unsupported crawler proxy scheme"):
            _build_crawler_proxies()

    def test_uses_one_proxy_from_file(self, monkeypatch, tmp_path):
        # The crawler shuffles its pool on purpose, so which line wins is
        # random. Assert the shape — one validated proxy used for both schemes
        # — rather than a fixed order, which made this test fail ~50% of runs.
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "# comment\nhttp://proxy1.example.com:8080\nhttp://proxy2.example.com:8081\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("CRAWLER_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_HTTP_PROXY", raising=False)
        monkeypatch.delenv("CRAWLER_HTTPS_PROXY", raising=False)
        monkeypatch.setenv("CRAWLER_PROXY_FILE", str(proxy_file))

        proxies = _build_crawler_proxies()

        assert proxies["http"] == proxies["https"]
        assert proxies["http"] in {
            "http://proxy1.example.com:8080",
            "http://proxy2.example.com:8081",
        }

    def test_uses_compact_webshare_proxy_file(self, monkeypatch, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "p.webshare.io:80:user1:pass1\np.webshare.io:80:user2:pass2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("CRAWLER_PROXY_FILE", str(proxy_file))

        proxies = _build_crawler_proxies()

        assert proxies["http"] == proxies["https"]
        assert proxies["http"] in {
            "http://user1:pass1@p.webshare.io:80",
            "http://user2:pass2@p.webshare.io:80",
        }

    def test_disable_proxy_short_circuits(self, monkeypatch):
        monkeypatch.setenv("CRAWLER_PROXY", "http://proxy.example.com:8080")

        assert _build_crawler_proxies(disable_proxy=True) is None


# --- crawl-attempt ledger (review #R7) -------------------------------------


@pytest.fixture(autouse=True)
def _clear_crawl_ledger_env(monkeypatch):
    monkeypatch.delenv("CRAWL_RETRY_AFTER_HOURS", raising=False)
    monkeypatch.delenv("CRAWL_MAX_ATTEMPTS", raising=False)


class TestCrawlRetryPolicy:
    def test_defaults_when_unset(self):
        assert _crawl_retry_policy() == (
            DEFAULT_CRAWL_RETRY_AFTER_HOURS,
            DEFAULT_CRAWL_MAX_ATTEMPTS,
        )

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("CRAWL_RETRY_AFTER_HOURS", "6")
        monkeypatch.setenv("CRAWL_MAX_ATTEMPTS", "9")

        assert _crawl_retry_policy() == (6, 9)

    def test_zero_max_attempts_disables_the_cap(self, monkeypatch):
        """0 is meaningful for max_attempts — 'never give up'."""
        monkeypatch.setenv("CRAWL_MAX_ATTEMPTS", "0")

        assert _crawl_retry_policy()[1] == 0

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "-1", "0", "1.5"])
    def test_bad_cooldown_falls_back_to_default(self, monkeypatch, bad):
        """A typo must not silently restore re-crawl-everything behavior.

        0 is invalid here (unlike max_attempts): a zero-hour cooldown would
        mean every depth iteration re-crawls, which is the exact bug the
        ledger exists to prevent.
        """
        monkeypatch.setenv("CRAWL_RETRY_AFTER_HOURS", bad)

        assert _crawl_retry_policy()[0] == DEFAULT_CRAWL_RETRY_AFTER_HOURS

    @pytest.mark.parametrize("bad", ["abc", "-4", "2.5"])
    def test_bad_max_attempts_falls_back_to_default(self, monkeypatch, bad):
        monkeypatch.setenv("CRAWL_MAX_ATTEMPTS", bad)

        assert _crawl_retry_policy()[1] == DEFAULT_CRAWL_MAX_ATTEMPTS


class TestIsWithinCooldown:
    def _cutoff(self, hours=24):
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    def test_never_crawled_is_not_in_cooldown(self):
        assert _is_within_cooldown(None, self._cutoff()) is False

    def test_recent_attempt_is_in_cooldown(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)

        assert _is_within_cooldown(recent, self._cutoff()) is True

    def test_old_attempt_is_not_in_cooldown(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)

        assert _is_within_cooldown(old, self._cutoff()) is False

    def test_naive_timestamp_is_treated_as_utc(self):
        """SQLite returns naive datetimes even for values written as aware.

        Comparing naive to aware raises TypeError, so the helper must
        normalize rather than crash mid-harvest.
        """
        naive_recent = datetime.now(timezone.utc).replace(tzinfo=None)

        assert _is_within_cooldown(naive_recent, self._cutoff()) is True


class TestRecordCrawlAttempt:
    def test_no_email_increments_attempts(self):
        biz = SimpleNamespace(last_crawled_at=None, crawl_attempts=2)
        stamp = datetime.now(timezone.utc)

        _record_crawl_attempt(biz, stamp, found_email=False)

        assert biz.crawl_attempts == 3
        assert biz.last_crawled_at == stamp

    def test_found_email_resets_attempts(self):
        """A site that starts publishing an address must not stay pinned
        at the give-up threshold."""
        biz = SimpleNamespace(last_crawled_at=None, crawl_attempts=7)
        stamp = datetime.now(timezone.utc)

        _record_crawl_attempt(biz, stamp, found_email=True)

        assert biz.crawl_attempts == 0
        assert biz.last_crawled_at == stamp

    def test_null_attempts_column_is_treated_as_zero(self):
        """Rows migrated from the pre-ledger schema can read back NULL."""
        biz = SimpleNamespace(last_crawled_at=None, crawl_attempts=None)

        _record_crawl_attempt(biz, datetime.now(timezone.utc), found_email=False)

        assert biz.crawl_attempts == 1


class TestHarvestLedgerAgainstDb:
    """End-to-end guard for #R7 against a real SQLite file.

    Before the ledger, a business crawled with no email found left no trace
    (the skip-set was built from contacts with a non-null email), so it was
    re-fetched on every depth iteration and every re-run of the pipeline.
    """

    @pytest.fixture
    def db(self, monkeypatch, tmp_path):
        """Point the engine at a throwaway file and build the schema.

        Two names need patching: harvest_emails_from_websites() imports
        `engine` from app.db.database at call time, but create_tables binds
        it at module import, so patching only one leaves them disagreeing.
        """
        from sqlalchemy import create_engine

        import app.db.database as db_module
        import app.db.create_tables as tables_module

        engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
        monkeypatch.setattr(db_module, "engine", engine)
        monkeypatch.setattr(tables_module, "engine", engine)
        tables_module.init_db()
        return tables_module, engine

    def _session(self, engine):
        from sqlalchemy.orm import sessionmaker

        return sessionmaker(bind=engine)()

    def _seed(self, tables_module, engine, count):
        session = self._session(engine)
        for i in range(count):
            session.add(tables_module.Business(
                business_name=f"biz{i}",
                website=f"http://ex{i}.com",
                domain=f"ex{i}.com",
            ))
        session.commit()
        session.close()

    def _harvest(self, monkeypatch, returns):
        """Run one harvest with the network stubbed; return crawled URLs."""
        crawled = []

        def fake_crawl(website, proxies=None):
            crawled.append(website)
            return returns(website)

        monkeypatch.setattr(extract_module, "_crawl_business", fake_crawl)
        extract_module.harvest_emails_from_websites()
        return crawled

    def test_second_harvest_skips_email_less_businesses(self, monkeypatch, db):
        """The #R7 regression guard: re-running must not re-crawl."""
        tables_module, engine = db
        self._seed(tables_module, engine, 4)
        no_emails = lambda _url: []

        first = self._harvest(monkeypatch, no_emails)
        second = self._harvest(monkeypatch, no_emails)

        assert len(first) == 4
        assert second == []

    def test_attempt_is_recorded_even_when_no_email_found(self, monkeypatch, db):
        tables_module, engine = db
        self._seed(tables_module, engine, 2)

        self._harvest(monkeypatch, lambda _url: [])

        session = self._session(engine)
        rows = session.query(tables_module.Business).all()
        assert all(b.last_crawled_at is not None for b in rows)
        assert [b.crawl_attempts for b in rows] == [1, 1]
        session.close()

    def test_crawl_error_still_counts_as_an_attempt(self, monkeypatch, db):
        """A reliably-erroring domain would otherwise be retried forever."""
        tables_module, engine = db
        self._seed(tables_module, engine, 1)

        def boom(_website, _proxies=None):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(extract_module, "_crawl_business", boom)
        extract_module.harvest_emails_from_websites()

        session = self._session(engine)
        biz = session.query(tables_module.Business).one()
        assert biz.crawl_attempts == 1
        assert biz.last_crawled_at is not None
        session.close()

    def test_expired_cooldown_allows_a_retry(self, monkeypatch, db):
        tables_module, engine = db
        self._seed(tables_module, engine, 2)
        self._harvest(monkeypatch, lambda _url: [])

        # Backdate past a 1-hour cooldown.
        session = self._session(engine)
        for biz in session.query(tables_module.Business).all():
            biz.last_crawled_at = datetime.now(timezone.utc) - timedelta(hours=2)
        session.commit()
        session.close()
        monkeypatch.setenv("CRAWL_RETRY_AFTER_HOURS", "1")

        retried = self._harvest(monkeypatch, lambda _url: [])

        assert len(retried) == 2

    def test_max_attempts_stops_retrying(self, monkeypatch, db):
        tables_module, engine = db
        self._seed(tables_module, engine, 1)
        monkeypatch.setenv("CRAWL_RETRY_AFTER_HOURS", "1")
        monkeypatch.setenv("CRAWL_MAX_ATTEMPTS", "2")

        crawl_counts = []
        for _ in range(4):
            session = self._session(engine)
            for biz in session.query(tables_module.Business).all():
                if biz.last_crawled_at:
                    biz.last_crawled_at = (
                        datetime.now(timezone.utc) - timedelta(hours=2)
                    )
            session.commit()
            session.close()
            crawl_counts.append(len(self._harvest(monkeypatch, lambda _url: [])))

        # Two attempts allowed, then permanently skipped despite the
        # cooldown having expired each round.
        assert crawl_counts == [1, 1, 0, 0]

    def test_finding_an_email_resets_the_counter(self, monkeypatch, db):
        tables_module, engine = db
        self._seed(tables_module, engine, 1)
        monkeypatch.setenv("CRAWL_RETRY_AFTER_HOURS", "1")

        self._harvest(monkeypatch, lambda _url: [])
        session = self._session(engine)
        biz = session.query(tables_module.Business).one()
        assert biz.crawl_attempts == 1
        biz.last_crawled_at = datetime.now(timezone.utc) - timedelta(hours=2)
        session.commit()
        session.close()

        self._harvest(monkeypatch, lambda _url: ["found@ex0.com"])

        session = self._session(engine)
        biz = session.query(tables_module.Business).one()
        assert biz.crawl_attempts == 0
        session.close()

    def test_businesses_with_emails_are_not_recrawled(self, monkeypatch, db):
        """Pre-existing behavior must survive the ledger."""
        tables_module, engine = db
        self._seed(tables_module, engine, 1)
        monkeypatch.setenv("CRAWL_RETRY_AFTER_HOURS", "1")

        self._harvest(monkeypatch, lambda _url: ["found@ex0.com"])
        session = self._session(engine)
        biz = session.query(tables_module.Business).one()
        biz.last_crawled_at = datetime.now(timezone.utc) - timedelta(hours=2)
        session.commit()
        session.close()

        # Cooldown expired and attempts are 0, but it already has an email.
        assert self._harvest(monkeypatch, lambda _url: []) == []


class TestInitDbAdditiveMigrations:
    def test_init_db_adds_exported_at_to_legacy_export_history(self, monkeypatch, tmp_path):
        import app.db.database as db_module
        import app.db.create_tables as tables_module

        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        monkeypatch.setattr(db_module, "engine", engine)
        monkeypatch.setattr(tables_module, "engine", engine)

        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE contacts (id INTEGER PRIMARY KEY)"))
            conn.execute(text(
                "CREATE TABLE export_history ("
                "id INTEGER PRIMARY KEY, "
                "contact_id INTEGER, "
                "destination TEXT, "
                "dummy_test INTEGER"
                ")"
            ))
            conn.execute(text(
                "INSERT INTO export_history (id, contact_id, destination, dummy_test) "
                "VALUES (1, 1, 'legacy', 0)"
            ))

        tables_module.init_db()

        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("export_history")
        }
        assert "exported_at" in columns

        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE export_history SET exported_at = CURRENT_TIMESTAMP "
                "WHERE exported_at IS NULL"
            ))

        session = sessionmaker(bind=engine)()
        session.add(tables_module.ExportHistory(
            contact_id=2,
            destination="after_migration",
            exported_at=datetime.now(timezone.utc),
        ))
        session.commit()

        inserted = session.query(tables_module.ExportHistory).filter_by(
            destination="after_migration"
        ).one()
        assert inserted.exported_at is not None
        session.close()
