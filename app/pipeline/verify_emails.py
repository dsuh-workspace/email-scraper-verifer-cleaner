"""
Email verification via self-hosted Reacher (check-if-email-exists) backend.

The supported instance is now local: `./scripts/start_local_verifier.sh` runs
the Reacher backend in Docker on 127.0.0.1:8080. A remote Kamatera deployment
was the original host (Hetzner blocks outbound port 25 for new accounts by
default, Kamatera does not) and its deploy scripts still live in the sibling
repo autopilotlocal/email-verifier, but it is legacy — point REACHER_API_URL
at it only if you have re-provisioned it. The backend has no auth either way.

The Kamatera *deploy* scripts read KAMATERA_ACCESS_KEY / KAMATERA_SECRET_KEY
from .env, but those are only needed to (re)provision that server — not to
call the /v0/check_email endpoint. This module needs only REACHER_API_URL.

API contract:
    POST {REACHER_API_URL}
    body: {"to_email": "someone@example.com"}
    response (200): Reacher CheckEmailOutput — the field we care about is
        is_reachable ∈ {"safe", "risky", "invalid", "unknown"}
    See https://github.com/reacherhq/check-if-email-exists for the full shape.
"""

import logging
import os

import requests
from dotenv import load_dotenv

from app.logging_config import setup_logging

logger = logging.getLogger(__name__)

load_dotenv()

# Local Reacher backend by default. Override in .env to target a remote one.
REACHER_API_URL = os.getenv(
    "REACHER_API_URL",
    "http://127.0.0.1:8080/v0/check_email",
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
        logger.warning("Reacher request failed for %s: %s", email, e)
        return {"is_reachable": "unknown", "score": 25, "raw": {}}

    if response.status_code != 200:
        logger.warning(
            "Reacher returned HTTP %d for %s: %s",
            response.status_code, email, response.text[:200],
        )
        return {"is_reachable": "unknown", "score": 25, "raw": {}}

    try:
        data = response.json()
    except ValueError as e:
        logger.warning("Reacher returned non-JSON for %s: %s", email, e)
        return {"is_reachable": "unknown", "score": 25, "raw": {}}

    status = data.get("is_reachable", "unknown")
    if status not in _SCORE_BY_STATUS:
        status = "unknown"

    return {
        "is_reachable": status,
        "score": _SCORE_BY_STATUS[status],
        "raw": data,
    }


def verify_contacts_emails() -> None:
    """
    Pull every contact that has an email + no prior verification row,
    check each via Reacher, persist an EmailVerification row and update
    Contact.lead_status.
    """
    from sqlalchemy.orm import sessionmaker

    from app.db.database import engine
    from app.db.create_tables import Contact, EmailVerification

    Session = sessionmaker(bind=engine)
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

        logger.info(f"Verifying {len(contacts)} unverified contacts via Reacher.")
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
            logger.info(
                "Verified: %s -> Status: %s (Score: %d)",
                contact.email, status, score,
            )

            # Commit incrementally so a crash mid-run doesn't lose progress.
            if verifications_run % 25 == 0:
                session.commit()

        session.commit()
        logger.info(
            "Verification run finished. Processed %d emails.",
            verifications_run,
        )
    except Exception as e:
        session.rollback()
        logger.error(f"Error during email verification run: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    setup_logging()
    verify_contacts_emails()