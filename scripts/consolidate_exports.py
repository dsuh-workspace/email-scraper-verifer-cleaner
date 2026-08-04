"""Consolidate the eight San Jose exports into one _final CSV per vertical.

Reuses the pipeline's own placeholder blocklist (app.pipeline.extract_emails)
rather than reinventing filter rules, plus the two extensions that list is
currently missing (.avif/.ico) which is why asset filenames reached the
exports in the first place.

Output shape matches the Santa Clarita _final convention: the standard
14-column export header, one row per business, one email per business.
"""
import csv
import collections
import re
import sys

sys.path.insert(0, ".")
from app.pipeline.extract_emails import EMAIL_REGEX, EXCLUDE_DOMAINS, EXCLUDE_EXTENSIONS

# .avif/.ico are absent from the pipeline's EXCLUDE_EXTENSIONS — that gap is
# what let 9 Air Care asset filenames through into the 08-02 HVAC export.
ASSET_EXT = tuple(EXCLUDE_EXTENSIONS) + (".avif", ".ico", ".bmp", ".tiff")

# Placeholder domains seen in San Jose data that the blocklist doesn't cover.
EXTRA_BLOCKED = ("address.com", "@mail.com")

FREEMAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "icloud.com", "comcast.net", "sbcglobal.net", "att.net", "msn.com",
    "me.com", "live.com", "ymail.com", "pacbell.net", "verizon.net",
}

HVAC_RE = re.compile(
    r"hvac|air condition|heating|furnace|cooling|air duct|boiler", re.I)
PLUMB_RE = re.compile(
    r"plumb|rooter|drain|sewer|water heater|repipe|septic|hot water|"
    r"gas installation|water filter|water soften|pipe", re.I)

HEADER = ["Export Date", "Contact Name", "Email", "Phone", "Job Title",
          "Business Name", "Website", "Category", "Review Count",
          "Review Rating", "Address", "Status", "Description", "Place ID"]

SOURCES = [
    "data/leads_sanjose_plumbing_rerun_2026-08-02c_verified.csv",
    "data/archive/leads_export_sanjose_zipcode.csv",
    "data/archive/leads_plumbing_2026-08-02.csv",
    "data/archive/leads_sanjose_hvac_2026-08-01.csv",
    "data/archive/leads_sanjose_hvac_2026-08-02.csv",
    "data/archive/leads_sanjose_plumbing_2026-07-29.csv",
    "data/archive/san-jose_plumbing_2026-07-30.csv",
    "data/archive/san_jose_runs_all_contacts_2026-07-30.csv",
]

# The two 07-30 audit files are no-email-by-construction; skip them entirely.
ALIASES = {
    "Email": "email", "Business Name": "business_name", "Website": "website",
    "Phone": "phone", "Contact Name": "contact_name", "Job Title": "job_title",
    "Category": "category", "Review Count": "review_count",
    "Review Rating": "review_rating", "Address": "address",
    "Status": "status", "Description": "description", "Place ID": "place_id",
    "Export Date": "export_date",
}


def get(row, canonical):
    """Read a field whether the file uses Title Case or snake_case headers."""
    if canonical in row:
        return (row.get(canonical) or "").strip()
    return (row.get(ALIASES.get(canonical, ""), "") or "").strip()


def norm_domain(url):
    d = re.sub(r"^https?://", "", (url or "").strip().lower())
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0]


def is_junk(email):
    e = email.lower()
    if not EMAIL_REGEX.fullmatch(e):
        return "malformed"
    if e.endswith(ASSET_EXT):
        return "asset-filename"
    if any(b in e for b in EXCLUDE_DOMAINS) or any(b in e for b in EXTRA_BLOCKED):
        return "blocklisted-placeholder"
    local = e.split("@")[0]
    if local in ("your", "youremail", "example", "email", "yourname", "info@mysite"):
        return "blocklisted-placeholder"
    return None


def vertical(category, biz_name=""):
    """Assign by PRIMARY Google Maps category (first token); Maps lists the
    business's own primary category first, so a 'Plumber, ..., HVAC contractor'
    is a plumber that also does HVAC."""
    cats = [c.strip() for c in (category or "").split(",") if c.strip()]
    if cats:
        primary = cats[0]
        if HVAC_RE.search(primary):
            return "hvac"
        if PLUMB_RE.search(primary):
            return "plumbing"
        # Primary is off-target (e.g. "Construction company"); fall back to any
        # trade category present, preferring plumbing since these runs were
        # predominantly plumbing sweeps.
        joined = " , ".join(cats)
        if PLUMB_RE.search(joined):
            return "plumbing"
        if HVAC_RE.search(joined):
            return "hvac"
    # No usable category signal (e.g. bare "Contractor") — the business name
    # is the last resort. Catches e.g. "Plumbing Tech Repipe Specialists Inc"
    # whose only Maps category is "Contractor".
    if PLUMB_RE.search(biz_name):
        return "plumbing"
    if HVAC_RE.search(biz_name):
        return "hvac"
    return None


# Local-part preference within a domain tier. A business often exposes several
# addresses; back-office inboxes reach the wrong human for a sales pitch.
GOOD_LOCAL = re.compile(
    r"^(info|service|contact|sales|office|hello|admin|support|estimates?|"
    r"scheduling|dispatch|customerservice|inquiries)", re.I)
BAD_LOCAL = re.compile(
    r"^(employment|careers?|jobs|hr|recruiting|billing|accounting|invoices?|"
    r"receivables|payables|webmaster|privacy|legal|noreply|no-reply)", re.I)


def rank(email, website):
    """Lower is better. Domain-match beats freemail beats other domains;
    within a tier, a sales-reachable local part beats a back-office one."""
    edom = email.split("@")[-1].lower()
    local = email.split("@")[0].lower()
    sdom = norm_domain(website)
    if sdom and edom == sdom:
        tier = 0
    elif edom in FREEMAIL:
        tier = 3
    else:
        tier = 6
    if BAD_LOCAL.match(local):
        return tier + 2
    if GOOD_LOCAL.match(local):
        return tier
    return tier + 1


best = {}          # biz key -> (rank, row, vertical)
dropped = collections.Counter()
off_vertical = []

for path in SOURCES:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            email = get(row, "Email")
            if not email:
                continue
            reason = is_junk(email)
            if reason:
                dropped[reason] += 1
                continue
            cat = get(row, "Category")
            biz = get(row, "Business Name")
            v = vertical(cat, biz)
            if v is None:
                off_vertical.append((biz, cat[:60], email))
                dropped["off-vertical"] += 1
                continue
            key = get(row, "Place ID") or biz.lower()
            if not key:
                continue
            website = get(row, "Website")
            r = rank(email, website)
            out = {
                "Export Date": get(row, "Export Date"),
                "Contact Name": get(row, "Contact Name"),
                "Email": email.lower(),
                "Phone": get(row, "Phone") or row.get("business_phone", "") or "",
                "Job Title": get(row, "Job Title"),
                "Business Name": biz,
                "Website": website,
                "Category": cat,
                "Review Count": get(row, "Review Count"),
                "Review Rating": get(row, "Review Rating"),
                "Address": get(row, "Address"),
                "Status": get(row, "Status"),
                "Description": get(row, "Description"),
                "Place ID": get(row, "Place ID"),
            }
            prev = best.get(key)
            # Prefer better rank; on tie prefer the row with more fields filled.
            if prev is None or (r, -sum(1 for x in out.values() if x)) < (
                    prev[0], -sum(1 for x in prev[1].values() if x)):
                best[key] = (r, out, v)

# Second dedupe pass, by email. Multi-location businesses get one Place ID
# per branch but share a single inbox, so business-key dedupe alone still
# leaves the same address twice — which would mean two outreach emails.
by_email = {}
for r, out, v in best.values():
    e = out["Email"]
    prev = by_email.get(e)
    filled = sum(1 for x in out.values() if x)
    if prev is None or (r, -filled) < (prev[0], -sum(1 for x in prev[1].values() if x)):
        by_email[e] = (r, out, v)
collapsed = len(best) - len(by_email)
if collapsed:
    print(f"collapsed {collapsed} duplicate-email rows (multi-location businesses)\n")

by_vert = collections.defaultdict(list)
for r, out, v in by_email.values():
    by_vert[v].append((r, out))

for v, fname in (("hvac", "data/leads_sanjose_hvac_2026-08-03_final.csv"),
                 ("plumbing", "data/leads_sanjose_plumbing_2026-08-03_final.csv")):
    rows = sorted(by_vert[v], key=lambda t: (t[0], t[1]["Business Name"].lower()))
    with open(fname, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        for _, out in rows:
            w.writerow(out)
    tiers = collections.Counter(r // 3 for r, _ in rows)
    print(f"{fname}: {len(rows)} businesses  "
          f"(domain-match={tiers[0]}, freemail={tiers[1]}, other-domain={tiers[2]})")

print("\ndropped:", dict(dropped))
if off_vertical:
    print("\noff-vertical (excluded):")
    for b, c, e in sorted(set(off_vertical))[:20]:
        print(f"  {b[:38]:38s} | {c:40s} | {e}")
