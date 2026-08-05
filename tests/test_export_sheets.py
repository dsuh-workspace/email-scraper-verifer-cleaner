from datetime import datetime

from sqlalchemy.orm import sessionmaker

from app.pipeline import export_sheets, extract_emails, process_leads
from app.db.create_tables import Contact, Business, ExportHistory, EmailVerification, RawLead, ScrapeRun
from app.db.database import engine


class TestDeriveCsvPaths:
    def test_derives_sibling_paths_from_default(self):
        paths = export_sheets._derive_csv_paths("data/leads_plumbing_2026-08-02.csv")

        assert paths == {
            "all": "data/leads_plumbing_2026-08-02_all.csv",
            "deduped": "data/leads_plumbing_2026-08-02_deduped.csv",
            "verified": "data/leads_plumbing_2026-08-02_verified.csv",
        }

    def test_derives_sibling_paths_from_explicit_csv_path(self):
        paths = export_sheets._derive_csv_paths("/tmp/custom.csv")

        assert paths == {
            "all": "/tmp/custom_all.csv",
            "deduped": "/tmp/custom_deduped.csv",
            "verified": "/tmp/custom_verified.csv",
        }


class TestProcessLeadsProvenance:
    def test_records_first_scrape_run_on_new_business_and_contact(self):
        RawLead.__table__.drop(engine, checkfirst=True)
        Contact.__table__.drop(engine, checkfirst=True)
        Business.__table__.drop(engine, checkfirst=True)
        ScrapeRun.__table__.drop(engine, checkfirst=True)
        ScrapeRun.__table__.create(engine)
        Business.__table__.create(engine)
        Contact.__table__.create(engine)
        RawLead.__table__.create(engine)

        session = sessionmaker(bind=engine)()
        run = ScrapeRun(query="Plumber", location="San Jose, CA", status="completed")
        session.add(run)
        session.flush()
        session.add(
            RawLead(
                scrape_run_id=run.id,
                business_name="Acme Plumbing",
                website="https://acmeplumbing.example",
                phone="408-555-0100",
                email="owner@acmeplumbing.example",
            )
        )
        session.commit()
        # Capture before close(): commit expires the instance, and reading
        # run.id on a detached object raises DetachedInstanceError.
        run_id = run.id
        session.close()

        process_leads.process_and_deduplicate_leads()

        session = sessionmaker(bind=engine)()
        try:
            business = session.query(Business).one()
            contact = session.query(Contact).one()
            assert business.first_scrape_run_id == run_id
            assert contact.first_scrape_run_id == run_id
        finally:
            session.close()

    def test_records_first_scrape_run_on_crawl_discovered_contact(self):
        """Emails found by the website crawl must carry provenance too.

        Regression guard: `_persist_emails_for_business` used to omit
        first_scrape_run_id entirely, so every crawl-discovered email landed
        with NULL provenance and dropped out of contact-level lift tables.
        """
        Contact.__table__.drop(engine, checkfirst=True)
        Business.__table__.drop(engine, checkfirst=True)
        ScrapeRun.__table__.drop(engine, checkfirst=True)
        ScrapeRun.__table__.create(engine)
        Business.__table__.create(engine)
        Contact.__table__.create(engine)

        session = sessionmaker(bind=engine)()
        try:
            run = ScrapeRun(query="HVAC", location="Santa Clara, CA", status="completed")
            session.add(run)
            session.flush()
            run_id = run.id

            business = Business(business_name="Acme HVAC", domain="acmehvac.example")
            session.add(business)
            session.flush()

            added = extract_emails._persist_emails_for_business(
                session, business, ["info@acmehvac.example"], scrape_run_id=run_id
            )
            session.commit()

            assert added == 1
            contact = session.query(Contact).one()
            assert contact.email == "info@acmehvac.example"
            assert contact.first_scrape_run_id == run_id
        finally:
            session.close()


class TestExportRunOutputs:
    def test_only_deduped_export_records_history(self, monkeypatch, tmp_path):
        engine = export_sheets.engine
        Contact.__table__.drop(engine, checkfirst=True)
        Business.__table__.drop(engine, checkfirst=True)
        ExportHistory.__table__.drop(engine, checkfirst=True)
        EmailVerification.__table__.drop(engine, checkfirst=True)
        Business.__table__.create(engine)
        Contact.__table__.create(engine)
        EmailVerification.__table__.create(engine)
        ExportHistory.__table__.create(engine)

        session = export_sheets.Session()
        business = Business(business_name="Acme", domain="acme.com")
        session.add(business)
        session.flush()
        contact = Contact(
            business_id=business.id,
            email="owner@acme.com",
            name="Owner",
        )
        session.add(contact)
        session.flush()
        contact_id = contact.id
        session.add(
            EmailVerification(
                contact_id=contact_id,
                status="safe",
                score=95,
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr(export_sheets, "append_leads_to_google_sheets", lambda leads: False)

        paths = export_sheets.export_run_outputs(
            min_score=50,
            csv_path=str(tmp_path / "leads.csv"),
        )

        session = export_sheets.Session()
        try:
            history = session.query(ExportHistory).all()
        finally:
            session.close()

        assert len(history) == 1
        assert history[0].contact_id == contact_id
        assert paths == {
            "all": str(tmp_path / "leads_all.csv"),
            "deduped": str(tmp_path / "leads_deduped.csv"),
            "verified": str(tmp_path / "leads_verified.csv"),
        }
        assert (tmp_path / "leads_all.csv").exists()
        assert (tmp_path / "leads_deduped.csv").exists()
        assert (tmp_path / "leads_verified.csv").exists()
