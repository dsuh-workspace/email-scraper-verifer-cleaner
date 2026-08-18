import os
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("SALESHANDY_API_KEY")
if not api_key:
    print("Error: SALESHANDY_API_KEY is not set.")
    exit(1)

headers = {
    "x-api-key": api_key,
    "Content-Type": "application/json"
}

def to_html(text: str) -> str:
    paragraphs = text.strip().split("\n\n")
    html_parts = []
    for p in paragraphs:
        # replace double asterisks with <strong>
        formatted = p.replace("**", "<strong>", 1)
        while "**" in formatted:
            formatted = formatted.replace("**", "</strong>", 1)
        # replace single newlines inside paragraph with <br/>
        formatted = formatted.replace("\n", "<br/>")
        html_parts.append(f"<p>{formatted}</p>")
    return "".join(html_parts)

# Define the 4 sequences
SEQUENCES = [
    {
        "title": "HVAC — Owner Direct Outreach (5 Steps)",
        "steps": [
            {
                "absoluteDays": 1,
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
                "absoluteDays": 4,
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
                "absoluteDays": 8,
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
                "absoluteDays": 15,
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
                "absoluteDays": 22,
                "subject": "closing the loop for {{company}}",
                "body": """Hey {{First Name}},

I won’t keep filling your inbox after this. I know you’re busy running {{company}}, not reading cold emails.

I reached out because after-hours missed calls quietly leak thousands in high-ticket HVAC jobs over the course of a season.

**If you ever want a 24/7 backup assistant to capture those calls, you can test our demo line anytime at 472-244-1040**

If you'd ever like to see how it works for {{company}}, feel free to reply with a good time and number and I'll give you a call.

Wishing you and the {{company}} team a great season!

Best,"""
            },
        ]
    },
    {
        "title": "HVAC — Team & Office Direct Outreach (3 Steps)",
        "steps": [
            {
                "absoluteDays": 1,
                "subject": "quick question for the {{company}} team",
                "body": """Hey {{company}} team,

Quick question—who handles incoming phone overflow or after-5 PM calls when your office gets slammed during weather spikes?

We built a 24/7 AI voice agent specifically for HVAC contractors to handle evening calls and take the dispatch pressure off office staff.

It’s not a clunky phone tree—**you can test our live 30-second demo line right now at 472-244-1040 to hear how natural it sounds.**

If this sounds like something that could save your office team some hassle, who would be the best person there to pass this along to?

Best,"""
            },
            {
                "absoluteDays": 4,
                "subject": "after-hours call overflow at {{company}}",
                "body": """Hey {{company}} team,

Following up on this—how does your office currently manage calls that come in after hours or over the weekend?

Instead of walking into a pile of voicemails every morning to return, our AI receptionist answers 24/7, qualifies the emergency caller, and books the diagnostic visit directly on your schedule.

**You can test it on our 30-second demo line: 472-244-1040**

Could you pass this along to whoever manages your service department or office schedule?

Best,"""
            },
            {
                "absoluteDays": 8,
                "subject": "passing this to your service manager?",
                "body": """Hey {{company}} team,

Last check on this! If after-hours call overflow isn't an issue for your team right now, no worries at all.

**But if your front desk ever wants a 24/7 backup assistant to catch emergency dispatch calls after 5 PM, feel free to give our demo line a quick call: 472-244-1040**

Hope you guys have a great week!

Best,"""
            }
        ]
    },
    {
        "title": "Plumbing — Owner Direct Outreach (5 Steps)",
        "steps": [
            {
                "absoluteDays": 1,
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
                "absoluteDays": 4,
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
                "absoluteDays": 8,
                "subject": "capturing weekend plumbing jobs",
                "body": """Hey {{First Name}},

Weekend plumbing calls are some of the highest-margin jobs of the week, but answering the phone 24/7 burns out your team.

Our AI voice agent acts as your 24/7 frontline: it answers immediately, screens out price-shoppers, collects photos/details, and schedules the job directly.

**Test the live demo line: 661-605-3526**

If you'd like to see how it could work for {{company}}, just let me know what day/time works best and what number to reach you at, and I'll give you a call.

Best,"""
            },
            {
                "absoluteDays": 15,
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
                "absoluteDays": 22,
                "subject": "closing the loop for {{company}}",
                "body": """Hey {{First Name}},

I won’t keep following up after this—I know you’re busy managing jobs and running {{company}}.

**If capturing after-hours emergency plumbing calls ever becomes a priority, you can test our demo line anytime at 661-605-3526**

If you'd ever like to see how it works for {{company}}, feel free to reply with a good time and number and I'll give you a call.

All the best with {{company}} this year!

Best,"""
            }
        ]
    },
    {
        "title": "Plumbing — Team & Office Direct Outreach (3 Steps)",
        "steps": [
            {
                "absoluteDays": 1,
                "subject": "quick question for the team at {{company}}",
                "body": """Hey {{company}} team,

Quick question—who handles incoming phone calls when your lines get overwhelmed or after your office closes for the day?

We built a 24/7 AI voice assistant for plumbing companies to handle after-hours emergency calls and take the dispatch pressure off your front desk.

It’s not a robotic menu—**you can call our live 30-second demo line at 661-605-3526 to hear how natural it sounds.**

If this could save your office team some time, who would be the best person there to forward this to?

Best,"""
            },
            {
                "absoluteDays": 4,
                "subject": "after-hours calls at {{company}}",
                "body": """Hey {{company}} team,

Following up on this—how does your team currently handle plumbing calls after 5 PM or over the weekend?

Instead of letting emergency calls go to voicemail and scrambling to call them back the next morning, our AI assistant answers 24/7, takes down the customer's issue, and books the service call directly.

**Test it out on our 30-second demo line: 661-605-3526**

Could you pass this along to whoever oversees your service department or office operations?

Best,"""
            },
            {
                "absoluteDays": 8,
                "subject": "passing this to your service manager?",
                "body": """Hey {{company}} team,

Last try on this! If managing after-hours calls isn't a problem for your team right now, no worries at all.

**But if your office ever needs a 24/7 assistant to handle evening dispatch and peak call spikes, feel free to test our demo line: 661-605-3526**

Thanks for your time and have a great week!

Best,"""
            }
        ]
    }
]

print("=" * 65)
print("DEPLOYING 4 DIRECT EMAIL SEQUENCES TO SALESHANDY")
print("=" * 65)

deployed_sequences = []

for seq in SEQUENCES:
    title = seq["title"]
    print(f"\nCreating Sequence: '{title}'...")
    
    create_resp = requests.post(
        "https://open-api.saleshandy.com/v1/sequences",
        json={"title": title},
        headers=headers,
        timeout=15
    )
    
    if create_resp.status_code not in (200, 201):
        print(f"  [ERROR] Failed to create sequence: HTTP {create_resp.status_code} - {create_resp.text}")
        continue
    
    seq_data = create_resp.json().get("payload", {})
    seq_id = seq_data.get("sequenceId") or seq_data.get("id")
    print(f"  [SUCCESS] Sequence Created! ID: {seq_id}")
    
    # Add steps
    for step_idx, s in enumerate(seq["steps"], 1):
        step_payload = {
            "type": 1,
            "absoluteDays": s["absoluteDays"],
            "variants": [
                {
                    "payload": {
                        "subject": s["subject"],
                        "content": to_html(s["body"])
                    }
                }
            ]
        }
        
        step_resp = requests.post(
            f"https://open-api.saleshandy.com/v1/sequences/{seq_id}/steps",
            json=step_payload,
            headers=headers,
            timeout=15
        )
        
        if step_resp.status_code in (200, 201):
            print(f"    Step {step_idx} (Day {s['absoluteDays']}) added: '{s['subject']}'")
        else:
            print(f"    [WARN] Step {step_idx} failed: HTTP {step_resp.status_code} - {step_resp.text[:100]}")
    
    deployed_sequences.append({
        "title": title,
        "id": seq_id,
        "steps_count": len(seq["steps"])
    })

print("\n" + "=" * 65)
print("DEPLOYMENT COMPLETE — SUMMARY OF CREATED SEQUENCES")
print("=" * 65)
for ds in deployed_sequences:
    print(f"  • {ds['title']}")
    print(f"    ID: {ds['id']} | Steps: {ds['steps_count']} | URL: https://app.saleshandy.com/sequences/{ds['id']}\n")
