from datetime import datetime

from sqlalchemy.orm import sessionmaker

from app.pipeline import export_sheets, extract_emails, process_leads
from app.db.create_tables import Contact, Business, ExportHistory, EmailVerification, RawLead, ScrapeRun
from app.db.database import engine


class TestDeriveCsvPaths:
    def test_derives_sibling_paths_from_default(self):
        from pathlib import Path
        paths = export_sheets._derive_csv_paths("data/leads_plumbing_2026-08-02.csv")

        assert {k: str(Path(v)) for k, v in paths.items()} == {
            "all": str(Path("data/leads_plumbing_2026-08-02_all.csv")),
            "deduped": str(Path("data/leads_plumbing_2026-08-02_deduped.csv")),
            "verified": str(Path("data/leads_plumbing_2026-08-02_verified.csv")),
        }

    def test_derives_sibling_paths_from_explicit_csv_path(self):
        from pathlib import Path
        paths = export_sheets._derive_csv_paths("/tmp/custom.csv")

        assert {k: str(Path(v)) for k, v in paths.items()} == {
            "all": str(Path("/tmp/custom_all.csv")),
            "deduped": str(Path("/tmp/custom_deduped.csv")),
            "verified": str(Path("/tmp/custom_verified.csv")),
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


class TestRunCohortFilter:
    """`run_cohort_start` scopes exports to businesses.first_scrape_run_id >=
    cutoff — without it, a DB carrying a prior baseline exports the whole
    DB, not just new work (see data/archive/MISLABELED_wholedb_export_*)."""

    def _seed_two_cohorts(self, session):
        old_run = ScrapeRun(query="Plumber", location="San Jose, CA", status="completed")
        new_run = ScrapeRun(query="Plumber", location="Sunnyvale, CA", status="completed")
        session.add_all([old_run, new_run])
        session.flush()

        old_business = Business(
            business_name="Old Co", domain="old.example", first_scrape_run_id=old_run.id
        )
        new_business = Business(
            business_name="New Co", domain="new.example", first_scrape_run_id=new_run.id
        )
        session.add_all([old_business, new_business])
        session.flush()

        session.add_all([
            Contact(business_id=old_business.id, email="owner@old.example", name="Owner"),
            Contact(business_id=new_business.id, email="owner@new.example", name="Owner"),
        ])
        session.commit()
        return new_run.id

    def test_export_new_leads_scopes_to_cohort(self, tmp_path, monkeypatch):
        engine = export_sheets.engine
        for table in (Contact, Business, ScrapeRun, ExportHistory, EmailVerification):
            table.__table__.drop(engine, checkfirst=True)
        for table in (ScrapeRun, Business, Contact, EmailVerification, ExportHistory):
            table.__table__.create(engine)

        session = export_sheets.Session()
        try:
            new_run_id = self._seed_two_cohorts(session)
        finally:
            session.close()

        monkeypatch.setattr(export_sheets, "append_leads_to_google_sheets", lambda leads: False)

        def _exported_emails():
            session = export_sheets.Session()
            try:
                return {
                    email
                    for (email,) in session.query(Contact.email).join(
                        ExportHistory, ExportHistory.contact_id == Contact.id
                    )
                }
            finally:
                session.close()

        export_sheets.export_new_leads(csv_path=str(tmp_path / "unscoped.csv"))
        assert _exported_emails() == {"owner@old.example", "owner@new.example"}

        session = export_sheets.Session()
        try:
            session.query(ExportHistory).delete()
            session.commit()
        finally:
            session.close()

        export_sheets.export_new_leads(
            csv_path=str(tmp_path / "scoped.csv"), run_cohort_start=new_run_id
        )
        assert _exported_emails() == {"owner@new.example"}

    def test_export_run_outputs_all_file_scopes_to_cohort(self, tmp_path, monkeypatch):
        engine = export_sheets.engine
        for table in (Contact, Business, ScrapeRun, ExportHistory, EmailVerification):
            table.__table__.drop(engine, checkfirst=True)
        for table in (ScrapeRun, Business, Contact, EmailVerification, ExportHistory):
            table.__table__.create(engine)

        session = export_sheets.Session()
        try:
            new_run_id = self._seed_two_cohorts(session)
        finally:
            session.close()

        monkeypatch.setattr(export_sheets, "append_leads_to_google_sheets", lambda leads: False)

        export_sheets.export_run_outputs(
            csv_path=str(tmp_path / "leads.csv"), run_cohort_start=new_run_id
        )

        with open(tmp_path / "leads_all.csv", encoding="utf-8") as f:
            all_rows = f.read()
        assert "owner@new.example" in all_rows
        assert "owner@old.example" not in all_rows
