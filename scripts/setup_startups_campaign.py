"""
Setup B2B Startups Outbound campaign in Instantly.
Creates campaign with 4-email startup-focused sequence.
"""

import os
import sys
import json
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip3 install requests")
    sys.exit(1)

# Load .env
project_root = Path(__file__).parent.parent
env_file = project_root / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

API_KEY = os.environ.get("INSTANTLY_API_KEY", "")
if not API_KEY:
    print("ERROR: INSTANTLY_API_KEY not found")
    sys.exit(1)

BASE_URL = "https://api.instantly.ai/api/v2"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}


def api_request(method, endpoint, data=None, params=None):
    url = f"{BASE_URL}/{endpoint}"
    try:
        if method == "GET":
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        elif method == "PATCH":
            r = requests.patch(url, headers=HEADERS, json=data if data else {}, params=params, timeout=30)
        else:
            r = requests.post(url, headers=HEADERS, json=data if data else {}, params=params, timeout=30)
        r.raise_for_status()
        return r.json() if r.content else {}
    except requests.RequestException as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                body = e.response.text[:500]
            except:
                pass
        print(f"  API ERROR [{endpoint}]: {e}")
        if body:
            print(f"  Response: {body}")
        return None


CAMPAIGN_NAME = "B2B Startups Outbound"

# Step 1: List existing campaigns
print("Fetching existing campaigns...")
result = api_request("GET", "campaigns")
campaigns = result.get("items", []) if result else []

if not campaigns:
    print("  No campaigns found (or API error)")
else:
    for c in campaigns:
        print(f"  - {c.get('name')} (id: {c.get('id')}, status: {c.get('status')})")

# Step 2: Check if campaign already exists
campaign_id = None
for c in campaigns:
    if c.get("name") == CAMPAIGN_NAME:
        campaign_id = c.get("id")
        print(f"\n'{CAMPAIGN_NAME}' already exists (id: {campaign_id})")
        break

# Step 3: Create if needed
if not campaign_id:
    print(f"\nCreating '{CAMPAIGN_NAME}' campaign...")
    r = api_request("POST", "campaigns", data={
        "name": CAMPAIGN_NAME,
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "Default",
                    "days": {
                        "1": True, "2": True, "3": True, "4": True, "5": True,
                        "0": False, "6": False
                    },
                    "timezone": "America/Detroit",
                    "timing": {"from": "09:00", "to": "17:00"}
                }
            ]
        }
    })
    if r:
        campaign_id = r.get("id")
        print(f"  Created (id: {campaign_id})")
    else:
        print("  ERROR: Failed to create campaign")
        sys.exit(1)

# Step 4: Set email sequences
print("\nSetting up email sequences...")

sequences = [
    {
        "steps": [
            {
                "type": "email",
                "delay": 0,
                "delay_unit": "days",
                "pre_delay_unit": "days",
                "variants": [
                    {
                        "subject": "This email found {{CompanyName}} automatically",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{personalized_opener}}</div><div><br /></div><div>Here's the thing: this email found {{companyname}}, researched your company, and landed here — all automatically. No SDR, no VA, no manual work.</div><div><br /></div><div>That's what I build: automated outbound systems for B2B companies. Your ideal prospects get researched, contacted, and followed up with — while you focus on closing.</div><div><br /></div><div>Think of it as an outbound engine running 24/7 at a fraction of what an SDR costs.</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>Open to a quick look? https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Max</div>"
                    },
                    {
                        "subject": "{{firstname}}, a question about {{CompanyName}}'s outbound",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{personalized_opener}}</div><div><br /></div><div>Here's the thing: this email found {{companyname}}, researched your company, and landed here — all automatically. No SDR, no VA, no manual work.</div><div><br /></div><div>That's what I build: automated outbound systems for B2B companies. Your ideal prospects get researched, contacted, and followed up with — while you focus on closing.</div><div><br /></div><div>Think of it as an outbound engine running 24/7 at a fraction of what an SDR costs.</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>Open to a quick look? https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Max</div>"
                    },
                    {
                        "subject": "How {{CompanyName}} could automate outbound",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{personalized_opener}}</div><div><br /></div><div>Here's the thing: this email found {{companyname}}, researched your company, and landed here — all automatically. No SDR, no VA, no manual work.</div><div><br /></div><div>That's what I build: automated outbound systems for B2B companies. Your ideal prospects get researched, contacted, and followed up with — while you focus on closing.</div><div><br /></div><div>Think of it as an outbound engine running 24/7 at a fraction of what an SDR costs.</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>Open to a quick look? https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Max</div>"
                    }
                ]
            },
            {
                "type": "email",
                "delay": 3,
                "delay_unit": "days",
                "pre_delay_unit": "days",
                "variants": [
                    {
                        "subject": "Re: This email found {{CompanyName}} automatically",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>Short version: we build outbound systems that research prospects, write personalized emails, and follow up — automatically. No SDRs to hire, train, or manage.</div><div><br /></div><div>Here's a 2-min walkthrough of how it works: https://zelusottomayor.com/demo</div><div><br /></div><div>Best,</div><div>Max</div>"
                    }
                ]
            },
            {
                "type": "email",
                "delay": 7,
                "delay_unit": "days",
                "pre_delay_unit": "days",
                "variants": [
                    {
                        "subject": "Re: This email found {{CompanyName}} automatically",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{industry_specific_insight}}</div><div><br /></div><div>Most startups I talk to are either doing founder-led outreach (which doesn't scale) or about to hire their first SDR (which takes 3-6 months to ramp). There's a third option — a system that runs in the background from day one.</div><div><br /></div><div>Happy to walk you through it: https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Max</div>"
                    }
                ]
            },
            {
                "type": "email",
                "delay": 12,
                "delay_unit": "days",
                "pre_delay_unit": "days",
                "variants": [
                    {
                        "subject": "Closing the loop",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>Timing not right — totally get it. If building a predictable pipeline ever moves up the priority list, the door's open.</div><div><br /></div><div>Cheers,</div><div>Max</div>"
                    }
                ]
            }
        ]
    }
]

r = api_request("POST", "campaigns/update/sequences", data={
    "campaign_id": campaign_id,
    "sequences": sequences
})
if r is not None:
    print("  Sequences configured successfully.")
else:
    print("  WARNING: Failed to set sequences via API.")
    print("  You may need to configure them manually in Instantly dashboard.")

# Step 5: Configure campaign settings
print("\nConfiguring campaign settings...")
patch_data = {
    "daily_limit": 200,
    "daily_max_leads": 50,
    "email_gap": 5,
    "random_wait_max": 3,
    "stop_on_reply": True,
    "stop_on_auto_reply": False,
    "link_tracking": False,
    "open_tracking": True,
    "text_only": False,
    "first_email_text_only": True,
    "match_lead_esp": True,
    "stop_for_company": False,
    "allow_risky_contacts": False,
    "disable_bounce_protect": False,
    "insert_unsubscribe_header": False,
    "email_list": ["max@zelusottomayor.com"]
}

r = api_request("PATCH", f"campaigns/{campaign_id}", data=patch_data)
if r is not None:
    print("  Campaign settings configured.")
else:
    print("  WARNING: Failed to set campaign settings.")
    print("  You may need to configure settings manually in Instantly.")

# Summary
print("\n" + "=" * 50)
print("SETUP COMPLETE")
print("=" * 50)
print(f"  Campaign: {CAMPAIGN_NAME}")
print(f"  Campaign ID: {campaign_id}")
print(f"  Sequences: 4 emails (Day 0, +3, +7, +12)")
print(f"  Email 1: 3 subject line variants (A/B/C test)")
print(f"  Schedule: Mon-Fri, 9 AM - 5 PM EST")
print(f"  Sender: max@zelusottomayor.com")
print(f"  Settings: text-only first email, open tracking, no link tracking")
print(f"\n  NOTE: Campaign is NOT activated yet.")
print(f"  Go to Instantly dashboard to review and activate when ready.")
