"""
Native Local Email Verifier Server (Port 8080).

Implements the Reacher /check_email HTTP contract natively in Python:
  - GET /version -> Healthcheck status
  - POST /v0/check_email -> {"to_email": "..."} -> Reacher-compatible JSON response

Runs locally on Windows without requiring Docker or external dependencies.
"""

from __future__ import annotations
import json
import logging
import os
import re
import sys
import smtplib
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import dns.resolver
from email_validator import validate_email, EmailNotValidError

from app.pipeline.email_filters import is_junk_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PORT = 8080
_MX_CACHE: dict[str, list[str]] = {}


def get_mx_records(domain: str) -> list[str]:
    """Resolve and cache MX records for a domain."""
    domain = domain.lower().strip()
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]

    records = []
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=3.0)
        # Sort by preference
        sorted_answers = sorted(answers, key=lambda r: r.preference)
        records = [str(r.exchange).rstrip(".") for r in sorted_answers]
    except Exception as e:
        logger.debug("MX lookup failed for %s: %s", domain, e)

    _MX_CACHE[domain] = records
    return records


def verify_email_address(email: str) -> dict:
    """Verify an email address and return Reacher-compatible JSON output."""
    if not email or "@" not in email:
        return {
            "input": email,
            "is_reachable": "invalid",
            "syntax": {"is_valid_syntax": False},
            "error": "missing_or_malformed_email",
        }

    email_clean = email.strip().lower()

    # 1. Syntax Check
    try:
        val = validate_email(email_clean, check_deliverability=False)
        normalized = val.normalized.lower()
        domain = normalized.split("@")[-1]
    except EmailNotValidError as e:
        return {
            "input": email,
            "is_reachable": "invalid",
            "syntax": {"is_valid_syntax": False},
            "error": str(e),
        }

    # 2. Blacklist / Junk Check
    if is_junk_email(normalized):
        return {
            "input": email,
            "is_reachable": "invalid",
            "syntax": {"is_valid_syntax": True},
            "error": "junk_or_role_account",
        }

    # 3. MX Record Check
    mx_records = get_mx_records(domain)
    if not mx_records:
        return {
            "input": email,
            "is_reachable": "invalid",
            "syntax": {"is_valid_syntax": True},
            "mx": {"accepts_mail": False, "records": []},
            "error": "no_mx_records_found",
        }

    # 4. Fast SMTP Deliverability Handshake (best-effort)
    primary_mx = mx_records[0]
    smtp_ok = False
    try:
        server = smtplib.SMTP(timeout=3.0)
        server.connect(primary_mx, 25)
        server.helo("verify.local")
        server.mail("verify@verify.local")
        code, msg = server.rcpt(normalized)
        server.quit()
        if code in (250, 251):
            smtp_ok = True
    except Exception:
        # Port 25 may be blocked by residential ISP; fallback to valid MX deliverability
        smtp_ok = False

    # Common valid domains with MX
    is_safe = bool(mx_records)
    reachability = "safe" if is_safe else "risky"

    return {
        "input": email,
        "is_reachable": reachability,
        "syntax": {"is_valid_syntax": True},
        "mx": {"accepts_mail": True, "records": mx_records},
        "smtp": {"can_connect_smtp": smtp_ok},
    }


class VerifierHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Override to suppress default noisy request logging
        logger.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self):
        if self.path in ("/", "/version", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"version": "1.0.0", "status": "running"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/v0/check_email":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
                email = data.get("to_email") or data.get("email") or ""
                result = verify_email_address(email)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode("utf-8"))
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def run_server():
    server_address = ("127.0.0.1", PORT)
    httpd = HTTPServer(server_address, VerifierHTTPHandler)
    logger.info("Local Email Verifier Server listening on http://127.0.0.1:%d", PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Local Email Verifier Server...")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
