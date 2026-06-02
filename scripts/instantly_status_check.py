#!/usr/bin/env python3
"""Check Instantly lead statuses with proper mapping."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.eu_outreach import load_config
from src.outreach import InstantlyClient

config = load_config("config/settings.yaml")
client = InstantlyClient(api_key=config.get("instantly", {}).get("api_key", ""))

campaigns = client.list_campaigns()
for c in campaigns:
    cid = c.get("id")
    name = c.get("name", "?")
    leads = client.list_leads(campaign_id=cid)

    statuses = {}
    for lead in leads:
        s = lead.get("lead_status", lead.get("status", "?"))
        statuses[s] = statuses.get(s, 0) + 1

    print(f"\n{name} ({len(leads)} leads)")
    for s, count in sorted(statuses.items(), key=lambda x: str(x[0])):
        print(f"  status={s}: {count}")

    # Print a sample lead to see all fields
    if leads:
        sample = leads[0]
        print(f"  Sample lead keys: {sorted(sample.keys())}")
