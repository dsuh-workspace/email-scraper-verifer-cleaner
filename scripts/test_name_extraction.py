import sqlite3
import re

conn = sqlite3.connect('database/hvac_leads.db')
cur = conn.cursor()

cur.execute('''
    SELECT c.id, c.name, c.title, c.email, c.phone, c.lead_status, b.business_name, b.category, b.primary_trade
    FROM contacts c
    JOIN businesses b ON b.id = c.business_id
    JOIN email_verifications ev ON ev.contact_id = c.id
    WHERE c.first_scrape_run_id IN (97, 98, 99) AND ev.score >= 80
      AND c.id NOT IN (SELECT contact_id FROM export_history WHERE destination LIKE 'saleshandy%')
''')
unexported = cur.fetchall()

GENERIC_TERMS = {
    'info', 'support', 'contact', 'admin', 'service', 'office', 'sales',
    'team', 'help', 'mail', 'billing', 'hvac', 'repipe', 'plumb', 'plumbing',
    'heating', 'cooling', 'repair', 'dispatch', 'customercare', 'inquiry',
    'inquiries', 'estimates', 'quote', 'quotes', 'accounting', 'careers',
    'jobs', 'privacy', 'rooter', 'drain', 'air', 'tech', 'general', 'lead',
    'marketing', 'feedback', 'orders', 'reception', 'desk', 'frontdesk',
    'customer', 'corp', 'mechanical', 'commercial', 'residential'
}

COMMON_FIRST_NAMES = {
    'john', 'david', 'michael', 'chris', 'cris', 'brian', 'frank', 'renee', 'bill',
    'william', 'robert', 'james', 'richard', 'joseph', 'thomas', 'charles', 'dan',
    'daniel', 'matthew', 'anthony', 'mark', 'paul', 'steve', 'steven', 'andrew',
    'ken', 'kenneth', 'josh', 'joshua', 'kevin', 'brian', 'george', 'edward',
    'ronald', 'tim', 'timothy', 'jason', 'jeff', 'jeffrey', 'ryan', 'jacob', 'gary',
    'nicholas', 'eric', 'jonathan', 'stephen', 'larry', 'justin', 'scott', 'brandon',
    'ben', 'benjamin', 'sam', 'samuel', 'greg', 'gregory', 'alex', 'alexander',
    'patrick', 'jack', 'dennis', 'jerry', 'tyler', 'aaron', 'jose', 'adam', 'henry',
    'nathan', 'douglas', 'zachary', 'peter', 'kyle', 'walter', 'ethan', 'jeremy',
    'harold', 'keith', 'christian', 'roger', 'noah', 'gerald', 'carl', 'terry',
    'sean', 'austin', 'arthur', 'lawrence', 'jesse', 'dylan', 'bryan', 'joe',
    'jordan', 'billy', 'bruce', 'albert', 'willie', 'gabriel', 'logan', 'alan',
    'juan', 'wayne', 'roy', 'ralph', 'randy', 'eugene', 'vincent', 'russell',
    'louis', 'philip', 'bobby', 'johnny', 'bradley', 'ali', 'tony', 'edu', 'lub',
    'dominique', 'ed', 'heidi', 'brent', 'ted', 'tasos', 'nick', 'raymond', 'walter'
}

def extract_name_from_email(email: str, existing_name: str = '') -> tuple[str, str, str]:
    """
    Extracts (first_name, last_name, persona) from an email address.
    Returns:
      (first_name, last_name, "Owner" | "NonOwner")
    """
    if existing_name and existing_name.strip() not in ('Info/Office', 'Decision Maker', 'General Contact', ''):
        parts = existing_name.strip().split(maxsplit=1)
        return (parts[0].capitalize(), parts[1].capitalize() if len(parts) > 1 else '', 'Owner')

    if not email or '@' not in email:
        return ('there', '', 'NonOwner')

    prefix = email.split('@')[0].strip().lower()
    # Strip numbers at end (e.g. brianelmore2461 -> brianelmore)
    prefix_clean = re.sub(r'\d+$', '', prefix).strip('._-')

    if not prefix_clean or any(prefix_clean == g for g in GENERIC_TERMS) or prefix_clean.endswith(('service', 'services', 'hvac', 'plumbing', 'repair', 'repipe', 'supply', 'handyman', 'cleaning')):
        return ('there', '', 'NonOwner')

    # Handle dot/underscore/hyphen separated names (e.g., tasos.karoutas, john_doe, alan_pong)
    for sep in ('.', '_', '-'):
        if sep in prefix_clean:
            subparts = [p for p in prefix_clean.split(sep) if p and p not in GENERIC_TERMS]
            if len(subparts) >= 2:
                fn = subparts[0].capitalize()
                ln = subparts[1].capitalize()
                if len(fn) > 1 and len(ln) > 1:
                    return (fn, ln, 'Owner')
            elif len(subparts) == 1:
                fn = subparts[0].capitalize()
                if len(fn) > 1:
                    return (fn, '', 'Owner')

    # If the clean prefix matches a known first name
    if prefix_clean in COMMON_FIRST_NAMES:
        return (prefix_clean.capitalize(), '', 'Owner')

    # Check for compound names like brianelmore, michaelsmchen
    for fname in COMMON_FIRST_NAMES:
        if len(fname) >= 3 and prefix_clean.startswith(fname) and len(prefix_clean) > len(fname):
            lname = prefix_clean[len(fname):].capitalize()
            if len(lname) >= 2:
                return (fname.capitalize(), lname, 'Owner')

    # If it's a single word and not generic, treat as Owner/Name
    if prefix_clean.isalpha() and 3 <= len(prefix_clean) <= 12:
        return (prefix_clean.capitalize(), '', 'Owner')

    return ('there', '', 'NonOwner')

print(f'=== EXTRACTED NAMES & PERSONAS FROM 74 LEADS ===\n')
owner_count = 0
nonowner_count = 0

for row in unexported:
    cid, name, title, email, phone, status, bname, cat, trade_db = row
    fn, ln, persona = extract_name_from_email(email, name)
    bname_str = (bname or 'Unknown')[:35]
    email_str = (email or '')[:35]
    if persona == 'Owner':
        owner_count += 1
        full = f'{fn} {ln}'.strip()
        print(f'[OWNER]     : {bname_str:<35} | {email_str:<35} -> Extracted Name: \"{full}\"')
    else:
        nonowner_count += 1
        print(f'[NON-OWNER] : {bname_str:<35} | {email_str:<35} -> Extracted Name: \"Team\"')

print(f'\nTotal Owners Extracted   : {owner_count}')
print(f'Total Non-Owners (Teams) : {nonowner_count}')
conn.close()
