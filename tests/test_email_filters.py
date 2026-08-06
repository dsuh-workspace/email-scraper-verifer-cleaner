"""Guards that the three email-writing paths share one junk list.

Before 2026-08-06 each path carried its own blocklist and they drifted: the
crawler rejected 37 domains, ingest 18, export 14. The practical effect was
that 19 domains the crawler had always rejected still reached `contacts`
whenever the *scraper's* own email field supplied the address rather than the
website crawl — and the export list was silently acting as a third net.
"""

import pytest

from app.pipeline.email_filters import is_junk_email
from app.pipeline.export_sheets import _is_bad_outreach_email
from app.pipeline.extract_emails import extract_emails_from_html
from app.pipeline.process_leads import _parse_and_validate_emails

# Each of these was blocked by at least one path but not all three.
FORMERLY_DIVERGENT = (
    "webmaster@santaclarita.gov",
    "x@cloudfront.net",
    "a@googleusercontent.com",
    "b@imtresidential.com",
    "c@engrain.com",
    "d@eliteonlinemedia.com",
    "e@2pointagency.com",
    "impallari@gmail.com",  # localpart rule — domain is freemail
    "f@gmaiil.com",         # was export-only
    "g@tel-us.biz",         # was export-only
    "logo@2x.png",          # asset filename that survives validate_email
)


@pytest.mark.parametrize("email", FORMERLY_DIVERGENT)
def test_all_three_paths_reject(email):
    """Ingest, crawl, and export must agree — that is the whole point."""
    assert is_junk_email(email)
    assert _parse_and_validate_emails(email) == []
    assert extract_emails_from_html(f"contact us at {email} today") == []
    assert _is_bad_outreach_email(email)


@pytest.mark.parametrize(
    "email", ["owner@realplumbing.com", "info@acme-hvac.example"]
)
def test_real_addresses_survive_every_path(email):
    assert not is_junk_email(email)
    assert _parse_and_validate_emails(email) == [email]
    assert extract_emails_from_html(f"email {email}") == [email]
    assert not _is_bad_outreach_email(email)


def test_outreach_prefix_rule_stays_export_only():
    """careers@ is a real inbox — drop it at export, not at ingest.

    Filtering it earlier would lose the business entirely when that is the
    only address on the site; at export we merely decline to pitch it.
    """
    email = "careers@realplumbing.com"

    assert not is_junk_email(email)
    assert _parse_and_validate_emails(email) == [email]
    assert _is_bad_outreach_email(email)
