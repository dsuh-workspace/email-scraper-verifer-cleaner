import re
import requests
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import Business, Contact

Session = sessionmaker(bind=engine)

# Regex to find email addresses in HTML content
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}')

# Exclude common media files or false positives in matching
EXCLUDE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', '.webp', '.css', '.js')

def extract_emails_from_html(html_text):
    """
    Extracts all unique email addresses from HTML text, filtering out common false positives.
    """
    found = EMAIL_REGEX.findall(html_text)
    emails = set()
    for email in found:
        email_lower = email.lower()
        # Filter out false positives from web assets
        if not any(email_lower.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
            emails.add(email_lower)
    return list(emails)

def harvest_emails_from_websites():
    """
    Queries all businesses, visits their website URLs, extracts any emails,
    and saves them to the contacts table.
    """
    session = Session()
    try:
        # Get all businesses that have websites
        businesses = session.query(Business).filter(Business.website.isnot(None)).all()
        print(f"Checking {len(businesses)} businesses for website email extraction...")
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }

        emails_harvested = 0

        for biz in businesses:
            # Check if we already have emails for this business
            has_emails = session.query(Contact).filter(
                Contact.business_id == biz.id,
                Contact.email.isnot(None)
            ).first()
            
            if has_emails:
                continue
            
            url = biz.website.strip()
            print(f"Crawling website: {url} ...")
            
            try:
                # Add schema if missing
                if not url.startswith(('http://', 'https://')):
                    url = 'http://' + url
                
                # Fetch page with a 7 second timeout
                response = requests.get(url, headers=headers, timeout=7)
                
                if response.status_code == 200:
                    emails = extract_emails_from_html(response.text)
                    if emails:
                        print(f"  -> Found emails: {', '.join(emails)}")
                        
                        # Add each unique email as a contact
                        for email in emails:
                            # Verify if already exists
                            existing = session.query(Contact).filter(
                                Contact.business_id == biz.id,
                                Contact.email == email
                            ).first()
                            
                            if not existing:
                                # We can update existing placeholder contact, or insert a new one
                                new_contact = Contact(
                                    business_id=biz.id,
                                    name="Info/Office",
                                    phone=biz.phone,
                                    title="General Contact",
                                    email=email,
                                    lead_status="Not Contacted"
                                )
                                session.add(new_contact)
                                emails_harvested += 1
                        
                        # Delete any placeholder phone-only general contacts if we found emails
                        # (to keep contacts clean)
                        placeholders = session.query(Contact).filter(
                            Contact.business_id == biz.id,
                            Contact.email.is_(None)
                        ).all()
                        for p in placeholders:
                            session.delete(p)
                            
                        session.commit()
                else:
                    print(f"  -> Failed (Status: {response.status_code})")
            except Exception as e:
                print(f"  -> Failed to crawl website: {e}")
                
        print(f"Email harvesting completed. Harvested {emails_harvested} unique email contacts.")
        
    except Exception as e:
        session.rollback()
        print(f"Error during email harvesting: {e}")
        raise e
    finally:
        session.close()

if __name__ == "__main__":
    harvest_emails_from_websites()
