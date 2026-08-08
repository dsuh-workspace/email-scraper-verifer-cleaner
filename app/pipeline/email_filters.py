"""Canonical junk-email filters, shared by every path that writes an email.

There are three places an address can enter or leave the system, and each used
to carry its own blocklist:

  1. `process_leads.py`   — the scraper's own `emails` field, on ingest.
  2. `extract_emails.py`  — addresses the website crawler regexes out of HTML.
  3. `export_sheets.py`   — a last gate before a contact reaches outreach.

They drifted. As of the 2026-08-06 audit the crawler blocked 37 domains, ingest
blocked 18, and export blocked 14 — so 19 domains the crawler rejected
(`santaclarita.gov`, `imtresidential.com`, `cloudfront.net`,
`googleusercontent.com`, ...) still reached `contacts` whenever the *scraper*
supplied the address rather than the crawl, and the export list was quietly
acting as a third net for the leakage.

One list, three call sites. Add new junk here and every path picks it up.

Matching is by **substring** against the whole address, which is why entries
like `email.com` also cover `foo@email.com.au` and why `cdn.` catches any
CDN-ish host. That bluntness is deliberate — a false positive costs one lead,
a false negative costs an outreach email to a font foundry.

`export_sheets.py` layers its own `_BAD_EMAIL_PREFIXES` (careers@, jobs@,
webmaster@) on top. Those are an outreach-quality judgment, not junk detection:
`careers@realplumber.com` is a real inbox that we simply do not want to pitch,
so it stays local to the export path.
"""

# Filename extensions that survive the email regex. Retina asset names are the
# usual source: "logo@2x.png" parses as local-part "logo" at domain "2x.png",
# and `email_validator` accepts it.
ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf",
    ".webp", ".avif", ".ico", ".bmp", ".tiff", ".css", ".js",
)

# Substring match against the full address.
JUNK_EMAIL_SUBSTRINGS = (
    # --- tracking / CDN / build-tool noise --------------------------------
    "sentry.io",
    "sentry-cdn.com",
    "sentry-next.wixpress.com",
    "wixpress.com",
    "wix.com",
    "cloudflare.com",
    "cloudfront.net",
    "googleusercontent.com",
    "gstatic.com",
    "cdn.",
    # --- recurring non-business addresses found on crawled pages ----------
    # Web agencies, property managers, and font foundries whose address is
    # embedded in a template or stylesheet the business happens to use. The
    # tell is always the same: one address showing up on many unrelated
    # businesses.
    "latofonts.com",
    "astigmatic.com",
    "pixelspread.com",
    "rioradio.org",
    "imtresidential.com",
    "newapthome.com",
    "engrain.com",
    "santaclarita.gov",
    "2pointagency.com",
    # contact-form relay, not the business's own inbox. Found on two
    # unrelated Sunnyvale/Santa Clara plumbing sites (2026-08-04).
    "eliteonlinemedia.com",
    # --- documentation / theme placeholders -------------------------------
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "yourdomain.com",
    "your-domain.com",
    "mysite.com",
    "yoursite.com",
    "youremail.com",
    "your-email.com",
    "email.com",
    # "email@address.com" — theme boilerplate. Not covered by "email.com"
    # above, which is matched as a substring and stops at the "@".
    "address.com",
    # all-x placeholder ("xxx@xxx.xxx")
    "xxx.xxx",
    # --- template-site junk seen in scraped SJ / Santa Clarita data -------
    "gami.com",
    "test.com",
    "sample.com",
    # registrar / host placeholder shown by parked pages
    "godaddy.com",
    # --- typo'd and truncated domains observed in live exports ------------
    # Previously export-only, so they entered the DB freely and were dropped
    # only at the last step. "ndiscovered.com" is a truncation artifact and
    # as a substring also covers "undiscovered.com".
    "gmaiil.com",
    "ndiscovered.com",
    "tel-us.biz",
)

# Local-parts to drop regardless of domain. Font designers ship a contact
# address inside webfont license headers and CSS comments, so the crawler
# harvests it from any site embedding that font — on a freemail domain, which
# JUNK_EMAIL_SUBSTRINGS cannot filter without blocking real contractors. The
# foundry-domain equivalents (astigmatic.com, latofonts.com) are above.
EXCLUDE_LOCALPARTS = (
    "impallari",  # Pablo Impallari / Impallari Type
)


def is_junk_email(email: str) -> bool:
    """True if `email` should never become a Contact or reach outreach.

    Safe to call on unvalidated regex output: an address with no "@" is junk
    by definition here, so callers do not need to pre-check the shape.
    """
    if not email:
        return True
    candidate = email.strip().lower()
    if "@" not in candidate:
        return True
    if candidate.endswith(ASSET_EXTENSIONS):
        return True
    if any(bad in candidate for bad in JUNK_EMAIL_SUBSTRINGS):
        return True
    if candidate.partition("@")[0] in EXCLUDE_LOCALPARTS:
        return True
    return False
