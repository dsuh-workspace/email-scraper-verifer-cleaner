import os
import re
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

CALENDAR_URL = "https://autopilotlocal.neetocal.com/ai-agent-consultation"

def to_html(text: str) -> str:
    paragraphs = text.strip().split("\n\n")
    html_parts = []
    for p in paragraphs:
        formatted = p.replace("**", "<strong>", 1)
        while "**" in formatted:
            formatted = formatted.replace("**", "</strong>", 1)
        formatted = formatted.replace("\n", "<br/>")
        html_parts.append(f"<p>{formatted}</p>")
    return "".join(html_parts)

# The updated templates for Direct Owner sequences
HVAC_OWNER_STEPS = [
    {
        "step_num": 1,
        "subject": "after-hours calls at {{company}}",
        "body": """Hey {{First Name}},

Quick question—when a homeowner’s AC or furnace gives out at 7:30 PM, what happens when they call {{company}}?

Most HVAC contractors lose high-ticket emergency replacement jobs simply because callers hit a recorded voicemail after 5 PM. When an AC dies in the heat, homeowners won't leave a message—they just call the next contractor on Google.

We built a 24/7 AI voice agent specifically for HVAC contractors that answers instantly, qualifies the homeowner, and books emergency jobs directly onto your calendar—without having to hire an after-hours answering service.

**You can test the live demo line right now at 472-244-1040 to hear how natural it sounds.**

If you'd like to see how it works for {{company}}, just let me know a good day/time and what number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 2,
        "subject": "the math on missed HVAC calls",
        "body": """Hey {{First Name}},

Thought about what it actually costs when calls get missed during busy weather spikes?

Even missing just one emergency system replacement or heat pump install a month can cost $8,000+ in lost top-line revenue.

Our AI voice agent answers every call 24/7, handles scheduling, answers common pricing/service questions, and transfers urgent calls directly to your on-call tech when needed.

**Give the demo line a quick 30-second call to test it: 472-244-1040**

If you'd like to see how it works for {{company}}, just let me know a good day/time and what number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 3,
        "subject": "how to stop losing after-hours HVAC jobs",
        "body": """Hey {{First Name}},

Picture this scenario:

It’s 8:00 PM on a Friday. Your office is closed, but a homeowner's AC compressor just failed.

Instead of getting a generic answering service that says "someone will call you Monday", they reach an AI receptionist that knows your dispatch area, confirms their issue, and locks in their diagnostic appointment for Saturday morning.

By Monday morning, you have confirmed jobs on the schedule instead of missed calls.

**Give our demo line a test call at 472-244-1040 to hear it in action.**

If you'd like to see how it could work for {{company}}, just let me know what day/time works best and what number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 4,
        "subject": "\"I don't want a robot talking to my customers\"",
        "body": """Hey {{First Name}},

The most common hesitation I hear from HVAC owners is:
"I don't want my customers talking to a clunky robot."

That's a completely valid concern. Most people picture robotic phone menus from the 90s that frustrate customers and send them straight to competitors.

That's not what we build.

Before anything goes live, we map out your exact intake process—your service areas, diagnostic fees, equipment types, and when to transfer emergency calls. Then we build a custom prototype tailored to {{company}}.

We don't do long-term contracts; we build a prototype so you can test it risk-free before deciding.

**You can test what it actually sounds like on our demo line at 472-244-1040**

If you'd like us to set up a custom prototype for {{company}}, just reply with a good day/time and number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 5,
        "subject": "closing the loop for {{company}}",
        "body": """Hey {{First Name}},

I won’t keep filling your inbox after this. I know you’re busy running {{company}}, not reading cold emails.

I reached out because after-hours missed calls quietly leak thousands in high-ticket HVAC jobs over the course of a season.

**If you ever want a 24/7 backup assistant to capture those calls, you can test our demo line anytime at 472-244-1040**

If you'd ever like to see how it works for {{company}}, feel free to reply with a good time and number and I'll give you a call.

Wishing you and the {{company}} team a great season!

Best,"""
    }
]

PLUMBING_OWNER_STEPS = [
    {
        "step_num": 1,
        "subject": "after-hours plumbing calls at {{company}}",
        "body": """Hey {{First Name}},

When a homeowner has a burst pipe or water heater failure at 8:00 PM, what happens when they call {{company}}?

In plumbing, emergency calls don't wait. If a caller hits a voicemail or gets told to call back tomorrow, 9 times out of 10 they immediately dial the next plumber on Google.

We built a 24/7 AI voice assistant for plumbing companies that answers instantly, gathers job details (leak location, shutoff status), and books emergency jobs directly onto your board—without paying for a high-cost 24/7 live answering service.

**Give our demo line a quick call at 661-605-3526 to hear how it sounds.**

If you'd like to see how it works for {{company}}, just let me know a good day/time and what number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 2,
        "subject": "the cost of missed plumbing calls",
        "body": """Hey {{First Name}},

Ever estimated what gets lost when emergency calls slip through the cracks after hours?

A single main drain backup, sewer replacement, or tankless water heater install can be worth $3,500 – $10,000+. Losing even one a month to a competitor because no one answered the phone hurts the bottom line.

Our AI receptionist answers every call 24/7, qualifies the emergency, and books the appointment on your calendar.

**Test it right now on our demo line: 661-605-3526**

If you'd like to see how it works for {{company}}, just let me know a good day/time and what number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 3,
        "subject": "capturing weekend plumbing jobs",
        "body": """Hey {{First Name}},

Weekend plumbing calls are some of the highest-margin jobs of the week, but answering the phone 24/7 burns out your team.

Our AI voice agent acts as your 24/7 frontline: it answers immediately, screens out price-shoppers, collects photos/details, and schedules the job directly.

**Test the live demo line: 661-605-3526**

If you'd like to see how it could work for {{company}}, just let me know what day/time works best and what number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 4,
        "subject": "\"does this actually work for plumbing?\"",
        "body": """Hey {{First Name}},

Plumbers often ask: "How can AI know whether a job is a true emergency or just a dripping faucet?"

That's why we build your system around your specific business rules:
* We define what constitutes an emergency (e.g., active flood vs routine repair).
* We set your dispatch pricing and service zones.
* We configure emergency transfers directly to your on-call technician.

We don't lock you into long contracts—we build a working prototype so you can test it risk-free.

**You can test what it actually sounds like on our demo line at 661-605-3526**

If you'd like us to set up a custom prototype for {{company}}, just reply with a good day/time and number to reach you at, and I'll give you a call.

Best,"""
    },
    {
        "step_num": 5,
        "subject": "closing the loop for {{company}}",
        "body": """Hey {{First Name}},

I won’t keep following up after this—I know you’re busy managing jobs and running {{company}}.

**If capturing after-hours emergency plumbing calls ever becomes a priority, you can test our demo line anytime at 661-605-3526**

If you'd ever like to see how it works for {{company}}, feel free to reply with a good time and number and I'll give you a call.

All the best with {{company}} this year!

Best,"""
    }
]

import time

def api_request(method, url, **kwargs):
    max_retries = 5
    for attempt in range(max_retries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 400 and "Rate Limit" in resp.text:
            wait_time = (attempt + 1) * 2.0
            print(f"    [RATE LIMIT] Waiting {wait_time}s before retry...")
            time.sleep(wait_time)
            continue
        time.sleep(0.8) # Polite pacing between all calls
        return resp
    return resp

def update_sequence_steps(seq_id: str, title: str, steps_source):
    print(f"\nUpdating Sequence: '{title}' ({seq_id})...")
    r = api_request("GET", f"https://open-api.saleshandy.com/v1/sequences/{seq_id}/steps", headers=headers)
    if r.status_code != 200:
        print(f"  [ERROR] Failed to fetch steps: HTTP {r.status_code} - {r.text}")
        return
    live_steps = r.json().get("payload", [])
    
    # Sort live steps by step number
    live_steps.sort(key=lambda s: s.get("number", 0))
    
    for idx, template in enumerate(steps_source):
        if idx >= len(live_steps):
            print(f"  [WARN] Step {template['step_num']} does not exist on live sequence.")
            continue
        
        live_step = live_steps[idx]
        step_id = live_step["id"]
        variants = live_step.get("variants", [])
        if not variants:
            print(f"  [WARN] Step {live_step.get('number')} has no variants.")
            continue
        
        var_id = variants[0]["id"]
        html_content = to_html(template["body"])
        
        patch_payload = {
            "payload": {
                "subject": template["subject"],
                "content": html_content
            }
        }
        
        url = f"https://open-api.saleshandy.com/v1/sequences/{seq_id}/steps/{step_id}/variants/{var_id}"
        resp = api_request("PATCH", url, headers=headers, json=patch_payload)
        if resp.status_code in (200, 201):
            print(f"  [OK] Step {idx+1} updated -> Subject: '{template['subject']}'")
        else:
            print(f"  [ERROR] Step {idx+1} update failed: HTTP {resp.status_code} - {resp.text}")

def main():
    r = api_request("GET", "https://open-api.saleshandy.com/v1/sequences", headers=headers)
    seqs = r.json().get("payload", [])
    
    print("=" * 70)
    print("UPDATING SALESHANDY CAMPAIGNS VIA API")
    print("=" * 70)
    
    for s in seqs:
        title = s.get("title", "")
        seq_id = s["id"]
        
        # Update Direct Owner sequences
        if "HVAC — Owner Direct Outreach" in title or "HVAC  Owner Direct Outreach" in title:
            update_sequence_steps(seq_id, title, HVAC_OWNER_STEPS)
        elif "Plumbing — Owner Direct Outreach" in title or "Plumbing  Owner Direct Outreach" in title:
            update_sequence_steps(seq_id, title, PLUMBING_OWNER_STEPS)
        elif "HVAC Owner" in title:
            # Update HVAC Owner phone-angle sequences
            print(f"\nUpgrading HVAC Owner Sequence: '{title}' ({seq_id})")
            update_sequence_steps(seq_id, title, HVAC_OWNER_STEPS)
        elif "Plumbing Owner" in title:
            # Update Plumbing Owner phone-angle sequences
            print(f"\nUpgrading Plumbing Owner Sequence: '{title}' ({seq_id})")
            update_sequence_steps(seq_id, title, PLUMBING_OWNER_STEPS)
        else:
            print(f"Skipping Non-Owner/Team sequence '{title}' (already verified link-free).")

if __name__ == "__main__":
    main()
