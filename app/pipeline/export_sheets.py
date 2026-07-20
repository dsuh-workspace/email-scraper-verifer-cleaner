import os
import csv
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import Contact, Business, ExportHistory

from app.logging_config import get_logger, setup_logging

logger = get_logger(__name__)

load_dotenv()

Session = sessionmaker(bind=engine)

LEGACY_EXPORT_DESTINATION = "local_csv_leads"
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "mock")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")

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

def write_leads_to_local_csv(leads_to_export, csv_path="data/leads_export.csv"):
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

def export_new_leads(destination: str | None = None):
    """
    Finds contacts that haven't been exported yet,
    exports them to Sheets (or fallback CSV), and logs the export history.
    """
    session = Session()
    destination = destination or (
        SPREADSHEET_ID if SPREADSHEET_ID.lower() != "mock" else LEGACY_EXPORT_DESTINATION
    )
    
    try:
        # Query contacts that don't have an export history entry for this destination
        new_leads = session.query(Contact, Business).join(
            Business, Contact.business_id == Business.id
        ).filter(
            Contact.email.isnot(None),
            ~Contact.id.in_(
                session.query(ExportHistory.contact_id).filter(
                    ExportHistory.destination == destination,
                    ExportHistory.contact_id.isnot(None),
                )
            )
        ).all()
        
        if not new_leads:
            logger.info("No new leads to export.")
            return

        logger.info(f"Found {len(new_leads)} new leads to export.")
        # Try to write to Google Sheets first
        success = append_leads_to_google_sheets(new_leads)
        
        # Fall back to local CSV if Sheets fails or isn't configured
        if not success:
            success = write_leads_to_local_csv(new_leads)
            
        if success:
            # Log export history entries
            for contact, _ in new_leads:
                history = ExportHistory(
                    contact_id=contact.id,
                    destination=destination,
                    exported_at=datetime.utcnow(),
                )
                session.add(history)
            session.commit()
            logger.info(f"Export logging completed. Logged {len(new_leads)} entries to export_history.")
        else:
            logger.error("Export failed.")
    except Exception as e:
        session.rollback()
        logger.error(f"Error during export: {e}")
        raise e
    finally:
        session.close()

if __name__ == "__main__":
    setup_logging()
    export_new_leads()