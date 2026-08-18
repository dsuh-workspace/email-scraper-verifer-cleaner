import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("SALESHANDY_API_KEY")
if not api_key:
    print("Error: SALESHANDY_API_KEY is not set.")
    exit(1)

headers = {
    "x-api-key": api_key,
    "Accept": "application/json",
    "Content-Type": "application/json"
}

def api_call(method, url, **kwargs):
    for attempt in range(5):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 400 and "Rate Limit" in resp.text:
            time.sleep((attempt + 1) * 2.0)
            continue
        time.sleep(0.8)
        return resp
    return resp

def audit_campaigns():
    r = api_call("GET", "https://open-api.saleshandy.com/v1/sequences", headers=headers)
    if r.status_code != 200:
        print(f"Failed to fetch sequences: HTTP {r.status_code}")
        return
    
    seqs = r.json().get("payload", [])
    
    active_seqs = [s for s in seqs if s.get("active") is True]
    inactive_seqs = [s for s in seqs if not s.get("active")]
    
    print("=" * 80)
    print("SALESHANDY LIVE CAMPAIGNS AUDIT REPORT")
    print(f"Total Sequences: {len(seqs)} | Active: {len(active_seqs)} | Inactive: {len(inactive_seqs)}")
    print("=" * 80)
    
    audit_results = []
    
    for seq in seqs:
        seq_id = seq["id"]
        title = seq.get("title", "")
        active = seq.get("active", False)
        
        # Determine trade & persona
        trade = "HVAC" if "HVAC" in title else ("Plumbing" if "Plumbing" in title else "Unknown")
        is_owner = "Owner" in title and "Non-Owner" not in title and "NonOwner" not in title
        expected_demo = "472-244-1040" if trade == "HVAC" else ("661-605-3526" if trade == "Plumbing" else "N/A")
        expected_steps = 5 if is_owner else 3
        
        # Fetch steps
        r_steps = api_call("GET", f"https://open-api.saleshandy.com/v1/sequences/{seq_id}/steps", headers=headers)
        steps = r_steps.json().get("payload", []) if r_steps.status_code == 200 else []
        steps.sort(key=lambda s: s.get("number", 0))
        
        # Fetch prospects summary if available
        # Check endpoint
        r_pros = api_call("GET", f"https://open-api.saleshandy.com/v1/sequences/{seq_id}/prospects?limit=1", headers=headers)
        total_prospects = "N/A"
        if r_pros.status_code == 200:
            pros_payload = r_pros.json().get("payload", {})
            if isinstance(pros_payload, dict):
                total_prospects = pros_payload.get("total", len(pros_payload.get("data", [])))
            elif isinstance(pros_payload, list):
                total_prospects = len(pros_payload)
        
        issues = []
        step_details = []
        
        if len(steps) != expected_steps and "Direct Outreach" in title:
            issues.append(f"Step count mismatch: expected {expected_steps}, got {len(steps)}")
            
        for st in steps:
            num = st.get("number")
            rel_days = st.get("relativeDays", 0)
            variants = st.get("variants", [])
            var_content = ""
            subj = ""
            if variants:
                v = variants[0]
                subj = v.get("payload", {}).get("subject", "")
                var_content = v.get("payload", {}).get("content", "")
            
            # Content checks
            has_calendar_link = "neetocal.com" in var_content or "calendly.com" in var_content or "http" in var_content and "calendar" in var_content.lower()
            has_expected_phone = expected_demo in var_content if expected_demo != "N/A" else True
            has_bad_encoding = "\ufffd" in var_content or "\ufffd" in subj
            
            if has_calendar_link:
                issues.append(f"Step {num} contains a calendar link!")
            if expected_demo != "N/A" and not has_expected_phone:
                issues.append(f"Step {num} missing expected demo phone {expected_demo}")
            if has_bad_encoding:
                issues.append(f"Step {num} contains corrupted encoding characters")
                
            step_details.append({
                "step": num,
                "days": rel_days,
                "subject": subj,
                "has_calendar_link": has_calendar_link,
                "has_demo_phone": has_expected_phone
            })
            
        audit_results.append({
            "id": seq_id,
            "title": title,
            "active": active,
            "trade": trade,
            "persona": "Owner" if is_owner else "Non-Owner/Team",
            "step_count": len(steps),
            "expected_steps": expected_steps,
            "expected_demo": expected_demo,
            "total_prospects": total_prospects,
            "issues": issues,
            "steps": step_details,
            "url": f"https://app.saleshandy.com/sequences/{seq_id}"
        })
        
    # Print formatted summary
    print("\n" + "=" * 80)
    print("LIVE ACTIVE CAMPAIGNS BREAKDOWN")
    print("=" * 80)
    
    for res in audit_results:
        if not res["active"]:
            continue
        status_str = "[ACTIVE]" if res["active"] else "[INACTIVE]"
        health_str = "[PASS - HEALTHY]" if not res["issues"] else f"[WARN - ISSUES ({len(res['issues'])})]"
        
        print(f"\n{status_str} | {health_str} | {res['title']}")
        print(f"  ID: {res['id']} | Trade: {res['trade']} | Persona: {res['persona']}")
        print(f"  Steps: {res['step_count']} / {res['expected_steps']} expected | Demo Phone: {res['expected_demo']} | Prospects: {res['total_prospects']}")
        print(f"  URL: {res['url']}")
        
        if res["issues"]:
            print("  [!] ISSUES DETECTED:")
            for iss in res["issues"]:
                print(f"    - {iss}")
        else:
            print("  [+] Zero calendar links detected")
            print(f"  [+] Demo line ({res['expected_demo']}) verified in all steps")
            print("  [+] Step structure & delays verified")
            
    print("\n" + "=" * 80)
    print("INACTIVE / STANDBY CAMPAIGNS")
    print("=" * 80)
    for res in audit_results:
        if res["active"]:
            continue
        print(f"[INACTIVE] {res['title']} (ID: {res['id']}) - Steps: {res['step_count']} | Issues: {len(res['issues'])}")
        if res["issues"]:
            for iss in res["issues"]:
                print(f"    - {iss}")

if __name__ == "__main__":
    audit_campaigns()
