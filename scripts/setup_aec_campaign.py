"""
Setup AEC Business Development campaign in Instantly.
Pauses the old Agency Outreach campaign and creates the new one.
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


# Step 1: List existing campaigns
print("Fetching existing campaigns...")
result = api_request("GET", "campaigns")
campaigns = result.get("items", []) if result else []

if not campaigns:
    print("  No campaigns found (or API error)")
else:
    for c in campaigns:
        print(f"  - {c.get('name')} (id: {c.get('id')}, status: {c.get('status')})")

# Step 2: Pause "Agency Outreach" if it exists and isn't already paused
for c in campaigns:
    if c.get("name") == "Agency Outreach":
        cid = c.get("id")
        print(f"\nPausing 'Agency Outreach' (id: {cid})...")
        r = api_request("POST", f"campaigns/{cid}/pause")
        print("  Done." if r is not None else "  WARNING: Failed to pause.")
        break

# Step 3: Check if AEC campaign exists
aec_id = None
for c in campaigns:
    if c.get("name") == "AEC Business Development":
        aec_id = c.get("id")
        print(f"\n'AEC Business Development' already exists (id: {aec_id})")
        break

# Step 4: Create if needed
if not aec_id:
    print("\nCreating 'AEC Business Development' campaign...")
    r = api_request("POST", "campaigns", data={
        "name": "AEC Business Development",
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
        aec_id = r.get("id")
        print(f"  Created (id: {aec_id})")
    else:
        print("  ERROR: Failed to create campaign")
        sys.exit(1)

# Step 5: Set email sequences
# Uses Instantly's built-in merge tags: {{firstname}}, {{companyname}}, {{CompanyName}}
# Plus our custom variables: {{personalized_opener}}, {{specific_pain_point}}, {{industry_specific_insight}}
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
                        "subject": "New project pipeline for {{CompanyName}}",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{personalized_opener}}</div><div><br /></div><div>Here's the thing: this email found {{companyname}}, researched your firm, wrote itself, and landed in your inbox — all automatically.</div><div><br /></div><div>That's what I build for engineering and architecture firms: a managed business development system. It identifies potential clients, writes personalized introductions, sends them, and follows up — so your principals can focus on delivery, not prospecting.</div><div><br /></div><div>Think of it as having a dedicated BD coordinator filling your pipeline, for a fraction of the cost of a full-time hire.</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>If you're open to seeing how this would work for {{companyname}}, grab 20 minutes here: https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Ze Lu Sottomayor</div>"
                    },
                    {
                        "subject": "{{firstname}}, quick question about your pipeline",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{personalized_opener}}</div><div><br /></div><div>Here's the thing: this email found {{companyname}}, researched your firm, wrote itself, and landed in your inbox — all automatically.</div><div><br /></div><div>That's what I build for engineering and architecture firms: a managed business development system. It identifies potential clients, writes personalized introductions, sends them, and follows up — so your principals can focus on delivery, not prospecting.</div><div><br /></div><div>Think of it as having a dedicated BD coordinator filling your pipeline, for a fraction of the cost of a full-time hire.</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>If you're open to seeing how this would work for {{companyname}}, grab 20 minutes here: https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Ze Lu Sottomayor</div>"
                    },
                    {
                        "subject": "How {{CompanyName}} could win more private-sector work",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>{{personalized_opener}}</div><div><br /></div><div>Here's the thing: this email found {{companyname}}, researched your firm, wrote itself, and landed in your inbox — all automatically.</div><div><br /></div><div>That's what I build for engineering and architecture firms: a managed business development system. It identifies potential clients, writes personalized introductions, sends them, and follows up — so your principals can focus on delivery, not prospecting.</div><div><br /></div><div>Think of it as having a dedicated BD coordinator filling your pipeline, for a fraction of the cost of a full-time hire.</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>If you're open to seeing how this would work for {{companyname}}, grab 20 minutes here: https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Ze Lu Sottomayor</div>"
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
                        "subject": "Re: New project pipeline for {{CompanyName}}",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>Quick follow-up.</div><div><br /></div><div>{{specific_pain_point}}</div><div><br /></div><div>The short version of what we do: we build outbound systems that run in the background so your team doesn't have to spend time on prospecting. You get leads researched, contacted, and followed up with — automatically.</div><div><br /></div><div>If you're curious about how it works, here's a short video walkthrough:</div><div>https://zelusottomayor.com/demo</div><div><br /></div><div>What are your thoughts on this?</div><div><br /></div><div>Best,</div><div>Ze Lu Sottomayor</div>"
                    }
                ]
            },
            {
                "type": "email",
                "delay": 4,
                "delay_unit": "days",
                "pre_delay_unit": "days",
                "variants": [
                    {
                        "subject": "Re: New project pipeline for {{CompanyName}}",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>One more thought —</div><div><br /></div><div>{{industry_specific_insight}}</div><div><br /></div><div>Most firms I work with rely on referrals and word-of-mouth for new business. Both work, but neither is proactive — you're always waiting for the phone to ring.</div><div><br /></div><div>A system that runs in the background means your pipeline doesn't depend on timing or luck.</div><div><br /></div><div>Happy to walk you through it: https://zelusottomayor.com/book-call</div><div><br /></div><div>Best,</div><div>Ze Lu Sottomayor</div>"
                    }
                ]
            },
            {
                "type": "email",
                "delay": 5,
                "delay_unit": "days",
                "pre_delay_unit": "days",
                "variants": [
                    {
                        "subject": "Closing the loop",
                        "body": "<div>Hi {{firstname}},</div><div><br /></div><div>I'll assume the timing isn't right — no worries at all.</div><div><br /></div><div>If building a more predictable pipeline ever becomes a priority for {{companyname}}, the door's open.</div><div><br /></div><div>Cheers,</div><div>Ze Lu Sottomayor</div>"
                    }
                ]
            }
        ]
    }
]

r = api_request("POST", "campaigns/update/sequences", data={
    "campaign_id": aec_id,
    "sequences": sequences
})
if r is not None:
    print("  Sequences configured successfully.")
else:
    print("  WARNING: Failed to set sequences via API.")
    print("  You may need to configure them manually in Instantly dashboard.")

# Step 6: Configure campaign settings to match old campaign
print("\nConfiguring campaign settings...")
# Use PATCH to update campaign settings
try:
    url = f"{BASE_URL}/campaigns/{aec_id}"
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
        "email_list": ["zelu@zelusottomayor.com"]
    }
    r = requests.patch(url, headers=HEADERS, json=patch_data, timeout=30)
    r.raise_for_status()
    print("  Campaign settings configured.")
except Exception as e:
    print(f"  WARNING: Failed to set campaign settings: {e}")
    print("  You may need to configure settings manually in Instantly.")

# Summary
print("\n" + "=" * 50)
print("SETUP COMPLETE")
print("=" * 50)
print(f"  Campaign: AEC Business Development")
print(f"  Campaign ID: {aec_id}")
print(f"  Sequences: 4 emails (Day 0, +3, +4, +5)")
print(f"  Email 1: 3 subject line variants (A/B/C test)")
print(f"  Schedule: Mon-Fri, 9 AM - 5 PM EST")
print(f"  Settings: text-only first email, open tracking, no link tracking")
print(f"\n  NOTE: Campaign is NOT activated yet.")
print(f"  Go to Instantly dashboard to review and activate when ready.")
print(f"  Old 'Agency Outreach' campaign has been paused.")
