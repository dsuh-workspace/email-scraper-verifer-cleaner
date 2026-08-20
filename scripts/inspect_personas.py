import sqlite3
from collections import Counter

conn = sqlite3.connect('database/hvac_leads.db')
cur = conn.cursor()

cur.execute('''
    SELECT c.id, c.name, c.title, c.email, b.business_name
    FROM contacts c
    JOIN businesses b ON b.id = c.business_id
    JOIN email_verifications ev ON ev.contact_id = c.id
    WHERE c.first_scrape_run_id IN (97, 98, 99) AND ev.score >= 80
      AND c.id NOT IN (SELECT contact_id FROM export_history WHERE destination LIKE 'saleshandy%')
''')
unexported = cur.fetchall()

print(f'=== INSPECTING THE 72 UNEXPORTED CONTACTS ===')
names = Counter(r[1] for r in unexported)
titles = Counter(r[2] for r in unexported)
email_prefixes = Counter(r[3].split('@')[0] for r in unexported if r[3])

print('\nTop Names:')
for n, c in names.most_common(10):
    print(f'  * {n}: {c}')

print('\nTop Titles:')
for t, c in titles.most_common(10):
    print(f'  * {t}: {c}')

print('\nTop Email Prefixes:')
for p, c in email_prefixes.most_common(10):
    print(f'  * {p}@: {c}')

print('\nFirst 20 Raw Contacts:')
for r in unexported[:20]:
    print(f'  * Contact #{r[0]}: Name="{r[1]}" | Title="{r[2]}" | Email="{r[3]}" | Biz="{r[4]}"')

conn.close()
