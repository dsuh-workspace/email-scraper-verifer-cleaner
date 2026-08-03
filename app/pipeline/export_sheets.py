import logging
import os
import csv
from datetime import UTC, datetime
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import Contact, Business, ExportHistory, EmailVerification

from app.logging_config import setup_logging

logger = logging.getLogger(__name__)

load_dotenv()

Session = sessionmaker(bind=engine)

LEGACY_EXPORT_DESTINATION = "local_csv_leads"
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "mock")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
DEFAULT_CSV_PATH = "data/leads_export.csv"

# Google Sheets scopes
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def append_leads_to_google_sheets(leads_to_export):
    """
    Connects to the Google Sheets API using a service account credentials file
    and appends lead records to the specified spreadsheet.
    """
    # Attempt to import Google client libraries. 
    # If not installed, we'll fall back to local CSV mode.
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning("Google APIs client libraries not found. Falling back to local CSV export.")
        return False

    if not os.path.exists(CREDENTIALS_FILE):
        logger.warning(f"Credentials file '{CREDENTIALS_FILE}' not found. Falling back to local CSV export.")
        return False
        
    if SPREADSHEET_ID.lower() == "mock":
        logger.warning("SPREADSHEET_ID is set to 'mock'. Falling back to local CSV export.")
        return False

    try:
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE, scopes=SCOPES
        )
        service = build('sheets', 'v4', credentials=creds)
        sheet = service.spreadsheets()
        
        # Prepare rows to append
        # Headers: Name, Email, Phone, Title, Business Name, Website, Category, Review Count, Review Rating, Address, Status, Description, Place ID
        values = []
        for contact, biz in leads_to_export:
            values.append([
                contact.name,
                contact.email,
                contact.phone or biz.phone,
                contact.title,
                biz.business_name,
                biz.website,
                biz.category,
                str(biz.review_count) if biz.review_count is not None else "",
                str(biz.review_rating) if biz.review_rating is not None else "",
                biz.address or "",
                biz.status or "",
                biz.description or "",
                biz.place_id or ""
            ])

        body = {'values': values}

        # Append to Sheet1 (starting at Column A)
        range_name = 'Sheet1!A:M'
        result = sheet.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        
        logger.info(f"Successfully appended {result.get('updates').get('updatedRows')} rows to Google Sheet.")
        return True

    except Exception as e:
        logger.error(f"Error writing to Google Sheets API: {e}")
        return False

def write_leads_to_local_csv(leads_to_export, csv_path=DEFAULT_CSV_PATH):
    """
    Writes leads to a local CSV file (used as a fallback or local development mock).
    """
    logger.info("Writing %d leads to local CSV: %s", len(leads_to_export), csv_path)
    file_exists = os.path.exists(csv_path)

    try:
        # os.path.dirname("leads.csv") == "" — feeding that to os.makedirs
        # raises FileNotFoundError. Only create the parent when there is one.
        csv_dir = os.path.dirname(csv_path)
        if csv_dir:
            os.makedirs(csv_dir, exist_ok=True)
        
        with open(csv_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            # Write headers if file is being created
            if not file_exists:
                writer.writerow([
                    "Export Date", "Contact Name", "Email", "Phone",
                    "Job Title", "Business Name", "Website", "Category",
                    "Review Count", "Review Rating", "Address", "Status",
                    "Description", "Place ID"
                ])

            for contact, biz in leads_to_export:
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    contact.name,
                    contact.email,
                    contact.phone or biz.phone,
                    contact.title,
                    biz.business_name,
                    biz.website,
                    biz.category,
                    str(biz.review_count) if biz.review_count is not None else "",
                    str(biz.review_rating) if biz.review_rating is not None else "",
                    biz.address or "",
                    biz.status or "",
                    biz.description or "",
                    biz.place_id or ""
                ])
        logger.info("Local CSV export completed.")
        return True
    except Exception as e:
        logger.error(f"Failed to write to local CSV: {e}")
        return False

def _latest_verification_subquery(session):
    latest_ids = (
        session.query(
            EmailVerification.contact_id.label("cid"),
            func.max(EmailVerification.id).label("latest_id"),
        )
        .group_by(EmailVerification.contact_id)
        .subquery()
    )
    return (
        session.query(
            EmailVerification.contact_id.label("cid"),
            EmailVerification.score.label("score"),
        )
        .join(latest_ids, EmailVerification.id == latest_ids.c.latest_id)
        .subquery()
    )


def _build_export_query(
    session,
    *,
    destination: str,
    min_score: int = 0,
    exported_only: bool = False,
):
    query = session.query(Contact, Business).join(
        Business, Contact.business_id == Business.id
    )

    if exported_only:
        query = query.filter(Contact.email.isnot(None))
        query = query.filter(
            ~Contact.id.in_(
                session.query(ExportHistory.contact_id).filter(
                    ExportHistory.destination == destination,
                    ExportHistory.contact_id.isnot(None),
                )
            )
        )

    if min_score > 0:
        latest = _latest_verification_subquery(session)
        query = query.join(latest, latest.c.cid == Contact.id).filter(
            latest.c.score >= min_score
        )
        logger.info("Gating export at min_score=%d (verifier score).", min_score)

    return query


def _derive_csv_paths(csv_path: str | None) -> dict[str, str]:
    base_path = Path(csv_path or DEFAULT_CSV_PATH)
    stem = base_path.stem
    suffix = base_path.suffix or ".csv"
    parent = base_path.parent
    return {
        "all": str(parent / f"{stem}_all{suffix}"),
        "deduped": str(parent / f"{stem}_deduped{suffix}"),
        "verified": str(parent / f"{stem}_verified{suffix}"),
    }


def _export_csv_only(leads_to_export, csv_path: str) -> bool:
    if not leads_to_export:
        logger.info("No leads to write for %s.", csv_path)
        return True
    return write_leads_to_local_csv(leads_to_export, csv_path=csv_path)


def export_new_leads(
    destination: str | None = None,
    min_score: int = 0,
    csv_path: str | None = None,
):
    """
    Finds contacts that haven't been exported yet, exports them to Sheets
    (or fallback CSV), and logs the export history.

    When min_score > 0, gate exports by the latest EmailVerification.score
    for each contact. Contacts with no verification row are treated as
    score=0 (unverified) and skipped.
    """
    session = Session()
    destination = destination or (
        SPREADSHEET_ID if SPREADSHEET_ID.lower() != "mock" else LEGACY_EXPORT_DESTINATION
    )

    try:
        new_leads = _build_export_query(
            session,
            destination=destination,
            min_score=min_score,
            exported_only=True,
        ).all()

        if not new_leads:
            logger.info("No new leads to export.")
            return []

        logger.info(f"Found {len(new_leads)} new leads to export.")
        success = append_leads_to_google_sheets(new_leads)

        if not success:
            success = write_leads_to_local_csv(
                new_leads, csv_path=csv_path or DEFAULT_CSV_PATH
            )

        if success:
            for contact, _ in new_leads:
                history = ExportHistory(
                    contact_id=contact.id,
                    destination=destination,
                    exported_at=datetime.now(UTC),
                )
                session.add(history)
            session.commit()
            logger.info(
                f"Export logging completed. Logged {len(new_leads)} entries to export_history."
            )
            return new_leads

        logger.error("Export failed.")
        return []
    except Exception as e:
        session.rollback()
        logger.error(f"Error during export: {e}")
        raise e
    finally:
        session.close()


def export_run_outputs(
    destination: str | None = None,
    min_score: int = 0,
    csv_path: str | None = None,
):
    destination = destination or (
        SPREADSHEET_ID if SPREADSHEET_ID.lower() != "mock" else LEGACY_EXPORT_DESTINATION
    )
    paths = _derive_csv_paths(csv_path)

    session = Session()
    try:
        all_leads = _build_export_query(
            session,
            destination=destination,
            exported_only=False,
        ).all()
        verified_leads = _build_export_query(
            session,
            destination=destination,
            min_score=min_score,
            exported_only=True,
        ).all()
    finally:
        session.close()

    _export_csv_only(all_leads, paths["all"])
    deduped_leads = export_new_leads(
        destination=destination,
        min_score=0,
        csv_path=paths["deduped"],
    )
    _export_csv_only(verified_leads, paths["verified"])

    logger.info(
        "Exported run outputs: all=%d deduped=%d verified=%d",
        len(all_leads),
        len(deduped_leads),
        len(verified_leads),
    )

    return {
        "all": paths["all"],
        "deduped": paths["deduped"],
        "verified": paths["verified"],
    }

if __name__ == "__main__":
    setup_logging()
    export_new_leads()