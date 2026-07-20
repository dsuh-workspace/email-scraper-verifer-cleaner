"""
Email verification via self-hosted Reacher (check-if-email-exists) backend.

Live instance is deployed to Kamatera (see the autopilotlocal/email-verifier
repo, CLAUDE.md — Hetzner blocks outbound port 25 for new accounts by
default, Kamatera does not). The Reacher backend itself has no auth.

The Kamatera *deploy* scripts read KAMATERA_ACCESS_KEY / KAMATERA_SECRET_KEY
from .env, but those are only needed to (re)provision the server — not to
call the /v0/check_email endpoint. This module needs only REACHER_API_URL.

API contract:
    POST {REACHER_API_URL}
    body: {"to_email": "someone@example.com"}
    response (200): Reacher CheckEmailOutput — the field we care about is
        is_reachable ∈ {"safe", "risky", "invalid", "unknown"}
    See https://github.com/reacherhq/check-if-email-exists for the full shape.
"""

import os
import time

import requests
from dotenv import load_dotenv
from sqlalchemy.orm import sessionmaker

from app.db.database import engine
from app.db.create_tables import Contact, EmailVerification

load_dotenv()

Session = sessionmaker(bind=engine)

# Reacher backend on Kamatera. Override in .env for local dev / new deploys.
REACHER_API_URL = os.getenv(
    "REACHER_API_URL",
    "http://104.128.66.74:8080/v0/check_email",
)
REACHER_TIMEOUT_SEC = int(os.getenv("REACHER_TIMEOUT_SEC", "30"))

# Reacher does not return a numeric score, only a reachability bucket.
# Map the bucket back to an integer so the EmailVerification.score column
# stays meaningful for downstream sorting/filtering.
_SCORE_BY_STATUS = {
    "safe": 95,
    "risky": 50,
    "invalid": 10,
    "unknown": 25,
}

# Contact.lead_status vocabulary — unchanged from the archived BillionVerify
# implementation so downstream export/CRM logic keeps working.
_LEAD_STATUS_BY_STATUS = {
    "safe": "Verified",
    "risky": "Risky",
    "invalid": "Invalid",
    "unknown": "Unknown",
}


def verify_email_via_reacher(email: str) -> dict:
    """
    Call the Reacher backend for a single email.

    Returns a dict with:
        is_reachable: str  — one of safe/risky/invalid/unknown
        score:        int  — 0..100, derived from is_reachable
        raw:          dict — full Reacher response (for debugging)

    Never raises: network / parse errors fall back to unknown so the
    pipeline keeps going instead of crashing on one bad address.
    """
    if not email or "@" not in email:
        return {"is_reachable": "invalid", "score": 0, "raw": {}}

    try:
        response = requests.post(
            REACHER_API_URL,
            json={"to_email": email},
            headers={"Content-Type": "application/json"},
            timeout=REACHER_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        print(f"[Warning] Reacher request failed for {email}: {e}")
        return {"is_reachable": "unknown", "score": 25, "raw": {}}

    if response.status_code != 200:
        print(
            f"[Warning] Reacher returned HTTP {response.status_code} for "
            f"{email}: {response.text[:200]}"
        )
        return {"is_reachable": "unknown", "score": 25, "raw": {}}

    try:
        data = response.json()
    except ValueError as e:
        print(f"[Warning] Reacher returned non-JSON for {email}: {e}")
        return {"is_reachable": "unknown", "score": 25, "raw": {}}

    status = data.get("is_reachable", "unknown")
    if status not in _SCORE_BY_STATUS:
        status = "unknown"

    return {
        "is_reachable": status,
        "score": _SCORE_BY_STATUS[status],
        "raw": data,
    }


def verify_contacts_emails(batch_sleep_sec: float = 0.0) -> None:
    """
    Pull every contact that has an email + no prior verification row,
    check each via Reacher, persist an EmailVerification row and update
    Contact.lead_status.

    batch_sleep_sec: optional pause between requests. Reacher runs on our
    own server so we don't need to be gentle for the vendor's sake, but a
    small delay reduces the chance of getting our IP greylisted by target
    mail providers during a large run.
    """
    session = Session()
    try:
        contacts = (
            session.query(Contact)
            .filter(Contact.email.isnot(None))
            .filter(
                ~Contact.id.in_(session.query(EmailVerification.contact_id))
            )
            .all()
        )

        print(f"Verifying {len(contacts)} unverified contacts via Reacher.")

        verifications_run = 0
        for contact in contacts:
            result = verify_email_via_reacher(contact.email)
            status = result["is_reachable"]
            score = result["score"]

            # 1. Persist verification row
            verification = EmailVerification(
                contact_id=contact.id,
                status=status,
                score=score,
            )
            session.add(verification)

            # 2. Update contact lead status
            contact.lead_status = _LEAD_STATUS_BY_STATUS.get(status, "Unknown")

            verifications_run += 1
            print(
                f"Verified: {contact.email} -> "
                f"Status: {status} (Score: {score})"
            )

            # Commit incrementally so a crash mid-run doesn't lose progress.
            if verifications_run % 25 == 0:
                session.commit()

            if batch_sleep_sec > 0:
                time.sleep(batch_sleep_sec)

        session.commit()
        print(
            f"Verification run finished. Processed {verifications_run} emails."
        )
    except Exception as e:
        session.rollback()
        print(f"Error during email verification run: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    verify_contacts_emails()
