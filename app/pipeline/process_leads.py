import re
import urllib.parse
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import RawLead, Business, Contact

Session = sessionmaker(bind=engine)

def extract_domain(url_str):
    """
    Extracts the base domain (e.g., 'rotorooter.com') from a website URL.
    """
    if not url_str:
        return None
    try:
        url_str = url_str.strip()
        if not url_str.startswith(('http://', 'https://')):
            url_str = 'http://' + url_str
        parsed = urllib.parse.urlparse(url_str)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        # Remove port if present
        if ':' in domain:
            domain = domain.split(':')[0]
        return domain
    except Exception:
        return None

def normalize_phone(phone_str):
    """
    Normalizes phone numbers to standard E.164-like format (e.g., +1XXXXXXXXXX).
    """
    if not phone_str:
        return None
    phone_str = phone_str.strip()
    # Strip non-digits
    digits = re.sub(r'\D', '', phone_str)
    
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    
    return phone_str  # Return original stripped if it doesn't match standard US phone length

def process_and_deduplicate_leads():
    """
    Processes unprocessed raw leads:
    1. Cleans and normalizes business name, phone, website.
    2. Deduplicates against the 'businesses' table using domain or name+phone.
    3. Moves cleaned records to 'businesses'.
    4. Extracts emails/phones and adds them as 'contacts' linked to the business.
    """
    session = Session()
    try:
        # In this simple implementation, we select all raw leads.
        # To avoid duplicating work, we can check if the raw lead's details 
        # (domain/phone) already exist in the businesses/contacts tables.
        raw_leads = session.query(RawLead).all()
        print(f"Loaded {len(raw_leads)} raw leads for processing.")

        businesses_added = 0
        contacts_added = 0

        for raw in raw_leads:
            # 1. Normalize fields
            cleaned_name = raw.business_name.strip() if raw.business_name else None
            cleaned_phone = normalize_phone(raw.phone)
            cleaned_website = raw.website.strip() if raw.website else None
            domain = extract_domain(cleaned_website)

            if not cleaned_name:
                continue

            # 2. Check for duplicate business in database
            existing_business = None
            
            # Match by domain if available
            if domain:
                existing_business = session.query(Business).filter(Business.domain == domain).first()
            
            # Or match by exact name and normalized phone if no domain
            if not existing_business and cleaned_phone:
                existing_business = session.query(Business).filter(
                    Business.business_name == cleaned_name,
                    Business.phone == cleaned_phone
                ).first()

            # 3. Insert or fetch Business ID
            if existing_business:
                business_id = existing_business.id
                # Optionally update phone/website if missing in existing profile
                if not existing_business.phone and cleaned_phone:
                    existing_business.phone = cleaned_phone
                if not existing_business.website and cleaned_website:
                    existing_business.website = cleaned_website
                    existing_business.domain = domain
            else:
                new_business = Business(
                    business_name=cleaned_name,
                    category=raw.category,
                    website=cleaned_website,
                    domain=domain,
                    phone=cleaned_phone
                )
                session.add(new_business)
                session.flush()  # Flushes to DB to generate the ID
                business_id = new_business.id
                businesses_added += 1

            # 4. Process and insert Contacts
            # Raw lead might contain one or multiple emails separated by commas
            emails = []
            if raw.email:
                # Split and clean emails
                emails = [email.strip() for email in raw.email.split(",") if email.strip()]

            # We also treat the raw lead's phone number as a potential contact path
            # if we have no email, or alongside the email.
            if emails:
                for email in emails:
                    # Check if contact already exists for this business with this email
                    existing_contact = session.query(Contact).filter(
                        Contact.business_id == business_id,
                        Contact.email == email
                    ).first()
                    
                    if not existing_contact:
                        new_contact = Contact(
                            business_id=business_id,
                            name="Info/Office",  # Default placeholder name
                            phone=cleaned_phone,
                            title="General Contact",
                            email=email,
                            lead_status="Not Contacted"
                        )
                        session.add(new_contact)
                        contacts_added += 1
            else:
                # If no email, add a general phone contact if not exists
                existing_contact = session.query(Contact).filter(
                    Contact.business_id == business_id,
                    Contact.phone == cleaned_phone
                ).first() if cleaned_phone else None

                if not existing_contact and cleaned_phone:
                    new_contact = Contact(
                        business_id=business_id,
                        name="Info/Office",
                        phone=cleaned_phone,
                        title="General Contact",
                        email=None,
                        lead_status="Not Contacted"
                    )
                    session.add(new_contact)
                    contacts_added += 1

        session.commit()
        print(f"Leads processing completed.")
        print(f"Added {businesses_added} new businesses to 'businesses' table.")
        print(f"Added {contacts_added} new contacts to 'contacts' table.")

    except Exception as e:
        session.rollback()
        print(f"Error during lead processing: {e}")
        raise e
    finally:
        session.close()

if __name__ == "__main__":
    process_and_deduplicate_leads()
