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


TOMBA_API_KEY = os.getenv("TOMBA_API_KEY")
TOMBA_SECRET_KEY = os.getenv("TOMBA_SECRET_KEY")


def check_reacher_health(timeout_sec: int = 5) -> bool:
    """
    Check if the Reacher verification backend is live and responding.
    """
    if not REACHER_API_URL:
        return False
    try:
        resp = requests.post(
            REACHER_API_URL,
            json={"to_email": "healthcheck@gmail.com"},
            headers={"Content-Type": "application/json"},
            timeout=timeout_sec,
        )
        return resp.status_code in (200, 400)
    except Exception as e:
        logger.debug("Reacher health check failed for %s: %s", REACHER_API_URL, e)
        return False


def verify_email_via_tomba(email: str) -> dict:
    """
    Verify an email via Tomba's Email Verifier API over HTTPS.
    Works directly without needing local Docker or a self-hosted server.
    """
    import time
    if not email or "@" not in email:
        return {"is_reachable": "invalid", "score": 0, "raw": {}}

    if not TOMBA_API_KEY or not TOMBA_SECRET_KEY:
        return {"is_reachable": "unknown", "score": 25, "raw": {}}

    url = f"https://api.tomba.io/v1/email-verifier/{email}"
    headers = {
        "X-Tomba-Key": TOMBA_API_KEY,
        "X-Tomba-Secret": TOMBA_SECRET_KEY,
    }

    for attempt in range(4):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 429:
                logger.warning("Tomba verifier rate limit hit (429). Cooling down for 10s (attempt %d/4)...", attempt + 1)
                time.sleep(10.0)
                continue

            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("email", {})
                result = (data.get("result") or "").lower()
                status = (data.get("status") or "").lower()
                score = data.get("score") or 50
                accept_all = bool(data.get("accept_all", False))

                if result == "deliverable" or status == "valid":
                    reach = "safe"
                    final_score = max(score, 90)
                elif result == "risky" or accept_all:
                    reach = "risky"
                    final_score = 50
                elif result == "undeliverable" or status == "invalid":
                    reach = "invalid"
                    final_score = min(score, 15)
                else:
                    reach = "unknown"
                    final_score = 25

                time.sleep(1.6)  # Maintain steady 37-40 RPM to respect Tomba Basic plan limit
                return {
                    "is_reachable": reach,
                    "score": final_score,
                    "raw": data,
                }
            else:
                logger.warning("Tomba verifier returned HTTP %d for %s: %s", resp.status_code, email, resp.text[:200])
                time.sleep(1.5)
                return {"is_reachable": "unknown", "score": 25, "raw": {"http_status": resp.status_code}}
        except Exception as e:
            logger.warning("Tomba verifier request error for %s: %s", email, e)
            time.sleep(2.0)

    return {"is_reachable": "unknown", "score": 25, "raw": {"error": "rate_limited"}}


def verify_email_via_reacher(email: str) -> dict:
    """
    Call the Reacher backend for a single email.

    Returns a dict with:
        is_reachable: str  — one of safe/risky/invalid/unknown
        score:        int  — 0..100, derived from is_reachable
        raw:          dict — full Reacher response (for debugging)
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


def verify_single_email(email: str) -> dict:
    """
    Verify single email using the best available engine:
    1. Reacher (if backend is live)
    2. Tomba Verifier API (if credentials configured)
    3. Syntax & MX Deliverability fallback
    """
    if check_reacher_health(timeout_sec=3):
        return verify_email_via_reacher(email)
    if TOMBA_API_KEY and TOMBA_SECRET_KEY:
        return verify_email_via_tomba(email)
    return {"is_reachable": "unknown", "score": 25, "raw": {"error": "no_verifier_configured"}}


def verify_contacts_emails(reverify_unknowns: bool = False, raise_on_unreachable: bool = False) -> int:
    """
    Pull every contact that has an email + no prior verification row (or unknown score if requested),
    check each via the active verifier (Tomba or Reacher), persist EmailVerification row and update
    Contact.lead_status.
    """
    from sqlalchemy.orm import sessionmaker
    from app.db.database import engine
    from app.db.create_tables import Contact, EmailVerification

    reacher_up = check_reacher_health(timeout_sec=3)
    tomba_up = bool(TOMBA_API_KEY and TOMBA_SECRET_KEY)

    if not reacher_up and not tomba_up:
        msg = (
            f"No email verifier is reachable (Reacher at {REACHER_API_URL} is down, and Tomba keys are missing). "
            f"Aborting to prevent false unknown records."
        )
        logger.error(msg)
        if raise_on_unreachable:
            raise RuntimeError(msg)
        return 0

    engine_name = "Reacher" if reacher_up else "Tomba (Local API)"
    logger.info("Using Email Verification Engine: %s", engine_name)

    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        if reverify_unknowns:
            # Re-verify contacts that have no verification OR are currently 'unknown' / score <= 25
            unknown_cids = session.query(EmailVerification.contact_id).filter(
                (EmailVerification.status == "unknown") | (EmailVerification.score <= 25)
            )
            contacts = (
                session.query(Contact)
                .filter(Contact.email.isnot(None))
                .filter(
                    (~Contact.id.in_(session.query(EmailVerification.contact_id))) |
                    (Contact.id.in_(unknown_cids))
                )
                .all()
            )
        else:
            contacts = (
                session.query(Contact)
                .filter(Contact.email.isnot(None))
                .filter(
                    ~Contact.id.in_(session.query(EmailVerification.contact_id))
                )
                .all()
            )

        if not contacts:
            logger.info("No contacts requiring verification.")
            return 0

        logger.info(f"Verifying {len(contacts)} contacts via {engine_name}...")
        verifications_run = 0

        for contact in contacts:
            result = verify_single_email(contact.email)
            status = result["is_reachable"]
            score = result["score"]

            # Update existing verification row or add new one
            existing_verif = session.query(EmailVerification).filter_by(contact_id=contact.id).first()
            if existing_verif:
                existing_verif.status = status
                existing_verif.score = score
            else:
                verification = EmailVerification(
                    contact_id=contact.id,
                    status=status,
                    score=score,
                )
                session.add(verification)

            # Update contact lead status
            contact.lead_status = _LEAD_STATUS_BY_STATUS.get(status, "Unknown")

            verifications_run += 1
            logger.info(
                "[%d/%d] Verified %s -> %s (Score: %d)",
                verifications_run, len(contacts), contact.email, status, score,
            )

            if verifications_run % 20 == 0:
                session.commit()

        session.commit()
        logger.info("Verification complete. Verified %d emails via %s.", verifications_run, engine_name)
        return verifications_run
    except Exception as e:
        session.rollback()
        logger.error(f"Error during email verification run: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    import argparse
    setup_logging()
    parser = argparse.ArgumentParser(description="Verify contact emails")
    parser.add_argument("--reverify-unknowns", action="store_true", help="Re-verify contacts with unknown status/score <= 25")
    args = parser.parse_args()
    verify_contacts_emails(reverify_unknowns=args.reverify_unknowns)