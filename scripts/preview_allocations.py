import sys, os
sys.path.insert(0, os.path.abspath('.'))
from sqlalchemy.orm import sessionmaker
from app.db.database import engine
from app.pipeline.export_saleshandy import sort_database_into_12_buckets, SEQUENCE_ID_MAP

Session = sessionmaker(bind=engine)
session = Session()

buckets = sort_database_into_12_buckets(
    session=session,
    min_score=80,
    exclude_unexported=True,
    only_classified=False,
    global_dedupe_emails=True
)

print('=== TARGET SALESHANDY CAMPAIGN ALLOCATION PLAN ===\n')
total_to_push = 0
for perm_tag, leads in sorted(buckets.items()):
    if not leads:
        continue
    seq_id = SEQUENCE_ID_MAP.get(perm_tag, 'Unknown')
    seq_url = f'https://app.saleshandy.com/sequences/{seq_id}'
    total_to_push += len(leads)
    print(f'Campaign: {perm_tag} ({len(leads)} leads)')
    print(f'  Target Sequence ID : {seq_id}')
    print(f'  Live Sequence URL  : {seq_url}')
    print(f'  Sample Leads:')
    for lead in leads[:4]:
        comp = lead.get('Company')
        fn = lead.get('First Name')
        ln = lead.get('Last Name')
        em = lead.get('Email')
        name_display = f"{fn} {ln}".strip()
        print(f'    * {comp} | {name_display} <{em}>')
    if len(leads) > 4:
        print(f'    ... and {len(leads) - 4} more')
    print()

print('-' * 70)
print(f'Total Leads Ready for Deployment: {total_to_push}')
print('=' * 70)
session.close()
