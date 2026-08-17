"""
Restore corrupted business categories in database/hvac_leads.db from raw_leads.
"""
import sqlite3
import re

HVAC_RE = re.compile(r"hvac|heating|cooling|furnace|air condition|heat pump|boiler|ductwork|\ba/?c\b", re.IGNORECASE)
PLUMBING_RE = re.compile(r"plumb|drain|sewer|septic|water heater|rooter|repipe", re.IGNORECASE)

DB_PATH = 'database/hvac_leads.db'

def restore_categories():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Find businesses whose category was set to the boilerplate simulation strings
    cursor.execute("""
        SELECT b.id, b.business_name, b.category, b.website, b.phone
        FROM businesses b
        WHERE b.category IN (
            'HVAC contractor, Air conditioning repair service, Heating contractor',
            'Plumber, Drainage service, Water heater repair'
        )
    """)
    corrupted_businesses = cursor.fetchall()
    print(f"Found {len(corrupted_businesses)} businesses with boilerplate simulation categories.")

    restored_count = 0
    for b_id, b_name, old_cat, website, phone in corrupted_businesses:
        # Look for the best raw_lead match
        cursor.execute("""
            SELECT category FROM raw_leads
            WHERE (website = ? AND website != '')
               OR (phone = ? AND phone != '')
               OR (lower(business_name) = lower(?))
            ORDER BY id ASC
        """, (website, phone, b_name))
        raw_rows = cursor.fetchall()
        
        real_cat = None
        for (rc,) in raw_rows:
            if rc and rc.strip():
                real_cat = rc.strip()
                break
        
        # Fallback if raw_leads had no category
        if not real_cat:
            if PLUMBING_RE.search(b_name) and not HVAC_RE.search(b_name):
                real_cat = "Plumber"
            elif HVAC_RE.search(b_name) and not PLUMBING_RE.search(b_name):
                real_cat = "HVAC contractor"
            else:
                real_cat = old_cat
        
        cursor.execute("UPDATE businesses SET category = ? WHERE id = ?", (real_cat, b_id))
        restored_count += 1
        print(f"  Restored ID {b_id:3d} ('{b_name}'): '{old_cat[:25]}...' -> '{real_cat}'")

    conn.commit()
    conn.close()
    print(f"Successfully restored {restored_count} business categories in {DB_PATH}.")

if __name__ == "__main__":
    restore_categories()
