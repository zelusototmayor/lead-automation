#!/usr/bin/env python3
"""Detailed Instantly analysis using email/open/reply counts."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.eu_outreach import load_config
from src.outreach import InstantlyClient

config = load_config("config/settings.yaml")
client = InstantlyClient(api_key=config.get("instantly", {}).get("api_key", ""))

# Instantly status codes: -1=bounced/error, 0=paused, 1=active, 2=completed, 3=unsubscribed
STATUS_MAP = {-1: "Bounced", 0: "Paused", 1: "Active", 2: "Completed", 3: "Unsubscribed"}

campaigns = client.list_campaigns()
for c in campaigns:
    cid = c.get("id")
    name = c.get("name", "?")
    leads = client.list_leads(campaign_id=cid)

    contacted = 0  # has timestamp_last_contact
    opened = 0
    replied = 0
    bounced = 0
    not_yet_sent = 0

    for lead in leads:
        status = lead.get("status", 0)
        has_contact = bool(lead.get("timestamp_last_contact"))
        opens = lead.get("email_open_count", 0) or 0
        replies = lead.get("email_reply_count", 0) or 0

        if status == -1:
            bounced += 1
        elif has_contact:
            contacted += 1
            if replies > 0:
                replied += 1
            elif opens > 0:
                opened += 1
        else:
            not_yet_sent += 1

    print(f"\n{'=' * 50}")
    print(f"{name} — {len(leads)} total leads")
    print(f"{'=' * 50}")
    print(f"  Not yet sent:     {not_yet_sent}")
    print(f"  Contacted:        {contacted}")
    print(f"    ├─ Opened:      {opened}")
    print(f"    └─ Replied:     {replied}")
    print(f"  Bounced:          {bounced}")
