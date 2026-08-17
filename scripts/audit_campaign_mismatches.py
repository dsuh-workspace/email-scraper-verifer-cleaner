import os
import glob
import csv
import re

HVAC_RE = re.compile(r"hvac|heating|cooling|furnace|air condition|heat pump|boiler|ductwork|\ba/?c\b", re.IGNORECASE)
PLUMBING_RE = re.compile(r"plumb|drain|sewer|septic|water heater|rooter|repipe", re.IGNORECASE)

print("=== AUDITING RE-EXPORTED SALESHANDY CAMPAIGN CSVS ===")
csv_files = glob.glob('data/saleshandy_campaigns/*.csv')

mismatches_in_csv = []
total_checked = 0
for fpath in csv_files:
    fname = os.path.basename(fpath)
    expected_trade = "HVAC" if "hvac" in fname.lower() else "Plumbing"
    
    with open(fpath, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=2):
            total_checked += 1
            company = row.get("Company", "")
            contact_id = row.get("Contact ID", "")
            email = row.get("Email", "")
            trade = row.get("Trade", "")
            
            comp_hvac = bool(HVAC_RE.search(company))
            comp_plumb = bool(PLUMBING_RE.search(company))
            
            # Check for strong name contradiction
            if expected_trade == "HVAC" and comp_plumb and not comp_hvac:
                mismatches_in_csv.append({
                    "file": fname,
                    "row": row_idx,
                    "contact_id": contact_id,
                    "company": company,
                    "email": email,
                    "assigned_trade": trade,
                    "apparent_trade": "Plumbing",
                })
            elif expected_trade == "Plumbing" and comp_hvac and not comp_plumb:
                mismatches_in_csv.append({
                    "file": fname,
                    "row": row_idx,
                    "contact_id": contact_id,
                    "company": company,
                    "email": email,
                    "assigned_trade": trade,
                    "apparent_trade": "HVAC",
                })

print(f"Total leads checked across all 12 CSVs: {total_checked}")
print(f"Total strong trade mismatches in CSVs: {len(mismatches_in_csv)}")
for m in mismatches_in_csv:
    print(f"  [{m['file']}: Row {m['row']}] ID {m['contact_id']}: '{m['company']}' ({m['email']}) -> Assigned: {m['assigned_trade']}, Expected: {m['apparent_trade']}")
