import os
import requests
from dotenv import load_dotenv
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.db.create_tables import Contact, EmailVerification

load_dotenv()

Session = sessionmaker(bind=engine)

REACHER_API_URL = os.getenv("REACHER_API_URL", "https://api.reacher.email/v0/check_email")
REACHER_API_KEY = os.getenv("REACHER_API_KEY")

def verify_email_via_reacher(email: str) -> dict:
    """
    Sends verification request to Reacher API.
    If REACHER_API_KEY is not configured or set to 'mock', returns a simulated check.
    """
    if not REACHER_API_KEY or REACHER_API_KEY.lower() == "mock":
        # Mock Response for local testing
        print(f"Mocking verification for: {email}")
        
        # Simple domain-based validation rules for mock
        if "@" not in email:
            return {"is_reachable": "invalid", "score": 0}
        
        domain = email.split("@")[1]
        if domain in ["example.com", "test.com", "bogus.xyz"]:
            return {"is_reachable": "invalid", "score": 10}
        elif "gmail.com" in domain or "yahoo.com" in domain or "outlook.com" in domain:
            return {"is_reachable": "safe", "score": 95}
        
        # Default fallback for normal business domains
        return {"is_reachable": "safe", "score": 85}

    # Real API call
    try:
        headers = {
            "Authorization": REACHER_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {"to_email": email}
        
        response = requests.post(REACHER_API_URL, json=payload, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            # Reacher responds with: { "input": "...", "is_reachable": "safe", ... }
            return {
                "is_reachable": data.get("is_reachable", "unknown"),
                "score": data.get("score", 50)
            }
        else:
            print(f"Reacher API responded with error status: {response.status_code}")
            return {"is_reachable": "unknown", "score": 50}
            
    except Exception as e:
        print(f"Reacher API call exception: {e}")
        return {"is_reachable": "unknown", "score": 50}

def verify_contacts_emails():
    """
    Finds contacts with emails that haven't been verified yet,
    verifies them via Reacher, and records the results.
    """
    session = Session()
    try:
        # Get contacts with emails that don't have a record in email_verifications yet
        unverified_contacts = session.query(Contact).filter(
            Contact.email.isnot(None),
            ~Contact.id.in_(session.query(EmailVerification.contact_id))
        ).all()

        if not unverified_contacts:
            print("No new contact emails to verify.")
            return

        print(f"Found {len(unverified_contacts)} contacts to verify.")
        verifications_run = 0

        for contact in unverified_contacts:
            result = verify_email_via_reacher(contact.email)
            
            # Map Reacher is_reachable response to standard Lead status
            # Options from Reacher: safe, invalid, risky, unknown
            reacher_status = result["is_reachable"]
            score = result["score"]
            
            # 1. Insert verification entry
            verification = EmailVerification(
                contact_id=contact.id,
                status=reacher_status,
                score=score
            )
            session.add(verification)
            
            # 2. Update contact lead status
            if reacher_status == "safe":
                contact.lead_status = "Verified"
            elif reacher_status == "invalid":
                contact.lead_status = "Invalid"
            elif reacher_status == "risky":
                contact.lead_status = "Risky"
            else:
                contact.lead_status = "Unknown"

            verifications_run += 1
            print(f"Verified: {contact.email} -> Status: {reacher_status} (Score: {score})")

        session.commit()
        print(f"Verification run finished. Processed {verifications_run} emails.")

    except Exception as e:
        session.rollback()
        print(f"Error during email verification process: {e}")
        raise e
    finally:
        session.close()

if __name__ == "__main__":
    verify_contacts_emails()
