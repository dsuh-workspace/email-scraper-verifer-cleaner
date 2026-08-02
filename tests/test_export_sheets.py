from datetime import datetime

from app.pipeline import export_sheets
from app.db.create_tables import Contact, Business, ExportHistory, EmailVerification


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
