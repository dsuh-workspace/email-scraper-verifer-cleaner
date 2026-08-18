import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()
headers = {
    "x-api-key": os.getenv("SALESHANDY_API_KEY"),
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

r = api_call("GET", "https://open-api.saleshandy.com/v1/sequences", headers=headers)
seqs = r.json().get("payload", [])

for s in seqs:
    seq_id = s["id"]
    r_steps = api_call("GET", f"https://open-api.saleshandy.com/v1/sequences/{seq_id}/steps", headers=headers)
    if r_steps.status_code != 200:
        continue
    for st in r_steps.json().get("payload", []):
        step_id = st["id"]
        for v in st.get("variants", []):
            var_id = v["id"]
            subj = v["payload"].get("subject", "")
            content = v["payload"].get("content", "")
            
            clean_subj = subj.replace("\ufffd", "-").replace("—", "-").replace("–", "-")
            clean_content = content.replace("\ufffd", "—")
            
            if clean_subj != subj or clean_content != content:
                patch_url = f"https://open-api.saleshandy.com/v1/sequences/{seq_id}/steps/{step_id}/variants/{var_id}"
                resp = api_call("PATCH", patch_url, headers=headers, json={"payload": {"subject": clean_subj, "content": clean_content}})
                if resp.status_code in (200, 201):
                    print(f"Cleaned encoding on seq {seq_id} ({s.get('title')}), step {st.get('number')}")

print("All Saleshandy sequences sanitized successfully!")
