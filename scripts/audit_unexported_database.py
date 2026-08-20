import sqlite3
from collections import Counter

conn = sqlite3.connect('database/hvac_leads.db')
cur = conn.cursor()

# Query all verified contacts never exported to Saleshandy
cur.execute('''
    SELECT 
        c.id, 
        c.name, 
        c.email, 
        c.phone, 
        c.lead_status, 
        c.first_scrape_run_id,
        b.business_name, 
        b.primary_trade,
        ev.score
    FROM contacts c
    JOIN businesses b ON b.id = c.business_id
    JOIN email_verifications ev ON ev.contact_id = c.id
    WHERE ev.score >= 80
      AND c.email IS NOT NULL AND c.email != ''
      AND c.id NOT IN (SELECT contact_id FROM export_history WHERE destination LIKE 'saleshandy%')
''')
unexported_all = cur.fetchall()

print('=== UNEXPORTED VERIFIED LEADS ACROSS ENTIRE DATABASE ===')
print(f'Total Unexported Verified Contacts: {len(unexported_all)}')

status_counts = Counter(r[4] for r in unexported_all)
print('\nBreakdown by Phone Lead Status:')
for st, count in status_counts.most_common():
    st_name = st if st else "None / Empty"
    print(f'  * {st_name:<30} : {count:>4} leads')

run_counts = Counter(r[5] for r in unexported_all)
print('\nBreakdown by Scrape Run ID:')
for run_id, count in sorted(run_counts.items(), key=lambda x: x[0] or 0):
    print(f'  * Run #{str(run_id):<26} : {count:>4} leads')

# Also check how many contacts have NO email verification yet or score < 80
cur.execute('''
    SELECT c.lead_status, COUNT(*)
    FROM contacts c
    LEFT JOIN email_verifications ev ON ev.contact_id = c.id
    WHERE (ev.id IS NULL OR ev.score < 80)
      AND c.id NOT IN (SELECT contact_id FROM export_history WHERE destination LIKE 'saleshandy%')
    GROUP BY c.lead_status
''')
unverified_counts = cur.fetchall()
print('\nUnverified or Low-Score Contacts by Phone Status:')
for st, count in unverified_counts:
    st_name = st if st else "None / Empty"
    print(f'  * {st_name:<30} : {count:>4} leads')

# Check total contacts in DB
cur.execute('SELECT COUNT(*) FROM contacts')
total_contacts = cur.fetchone()[0]

cur.execute('SELECT COUNT(DISTINCT contact_id) FROM export_history WHERE destination LIKE "saleshandy%"')
total_exported_contacts = cur.fetchone()[0]

print('\nDatabase Grand Totals:')
print(f'  * Total Contacts in DB           : {total_contacts}')
print(f'  * Total Distinct Contacts Exported: {total_exported_contacts}')

conn.close()
