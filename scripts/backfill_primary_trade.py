"""
Backfill primary_trade for all businesses in database/hvac_leads.db
"""
import sqlite3
import re

HVAC_RE = re.compile(r"hvac|heating|cooling|furnace|air condition|heat pump|boiler|ductwork|\ba/?c\b", re.IGNORECASE)
PLUMBING_RE = re.compile(r"plumb|drain|sewer|septic|water heater|rooter|repipe", re.IGNORECASE)

def classify_trade_str(name: str, cat: str, desc: str = "") -> str:
    name = (name or "").strip()
    cat = (cat or "").strip()
    desc = (desc or "").strip()

    name_hvac = bool(HVAC_RE.search(name))
    name_plumb = bool(PLUMBING_RE.search(name))

    # 1. High-confidence explicit business name check
    if name_plumb and not name_hvac:
        return "Plumbing"
    if name_hvac and not name_plumb:
        return "HVAC"

    # 2. Check GMB categories
    cat_hvac = bool(HVAC_RE.search(cat))
    cat_plumb = bool(PLUMBING_RE.search(cat))
    if cat_plumb and not cat_hvac:
        return "Plumbing"
    if cat_hvac and not cat_plumb:
        return "HVAC"

    # 3. Combined text check
    text = f"{name} {cat} {desc}"
    if HVAC_RE.search(text) and not PLUMBING_RE.search(text):
        return "HVAC"
    if PLUMBING_RE.search(text) and not HVAC_RE.search(text):
        return "Plumbing"

    return "HVAC" if HVAC_RE.search(text) and not PLUMBING_RE.search(text) else "Plumbing"

conn = sqlite3.connect('database/hvac_leads.db')
cursor = conn.cursor()

cursor.execute("SELECT id, business_name, category, description, trade_override FROM businesses")
rows = cursor.fetchall()
print(f"Backfilling primary_trade for {len(rows)} businesses...")

counts = {"HVAC": 0, "Plumbing": 0}
for b_id, name, cat, desc, override in rows:
    trade = override if override else classify_trade_str(name, cat, desc)
    cursor.execute("UPDATE businesses SET primary_trade = ? WHERE id = ?", (trade, b_id))
    counts[trade] += 1

conn.commit()
conn.close()
print(f"Successfully backfilled primary_trade: {counts}")
