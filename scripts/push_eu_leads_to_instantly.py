#!/usr/bin/env python3
"""
Push existing EU B2B leads from Google Sheets → Instantly campaign.

Reads leads from the "EU B2B Leads" sheet that haven't been queued yet
(Status != "Queued" and no Instantly Status) and adds them to the
"EU B2B - hiring sales" campaign in Instantly.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.eu_outreach import load_config
from src.crm import GoogleSheetsCRM
from src.outreach import InstantlyClient


def main():
    config = load_config("config/settings.yaml")
    ec = config["eu_outreach"]

    # Connect to CRM
    crm = GoogleSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
        sheet_name=ec.get("sheet_name", "EU B2B Leads"),
    )

    # Get all leads from sheet (not just "New" — grab everything with an email)
    all_leads = []
    for row in crm._cache:
        lead = crm._row_to_dict(list(row))
        if lead.get("email") and lead.get("status") != "Queued" and not lead.get("instantly_status"):
            all_leads.append(lead)

    print(f"Found {len(all_leads)} leads not yet in Instantly")
    if not all_leads:
        print("Nothing to push.")
        return

    # Connect to Instantly and find campaign
    instantly = InstantlyClient(
        api_key=config.get("instantly", {}).get("api_key", ""),
    )

    campaign_name = ec.get("instantly", {}).get("campaign_name", "EU B2B - hiring sales")
    campaigns = instantly.list_campaigns()

    campaign_id = None
    for c in campaigns:
        if c.get("name") == campaign_name:
            campaign_id = c.get("id")
            break

    if not campaign_id:
        print(f"Campaign '{campaign_name}' not found in Instantly!")
        print("Available campaigns:")
        for c in campaigns:
            print(f"  - {c.get('name')} (id: {c.get('id')})")
        return

    print(f"Found campaign: {campaign_name} ({campaign_id})")
    print(f"Pushing {len(all_leads)} leads...")

    # Push leads to Instantly
    result = instantly.add_leads_to_campaign(campaign_id, all_leads)

    if result:
        print(f"\nSuccessfully pushed {len(result)} leads to Instantly!")
        # Update status in sheet
        updated = 0
        for lead in all_leads:
            if lead.get("id"):
                success = crm.update_lead(lead["id"], {"status": "Queued"})
                if success:
                    updated += 1
        print(f"Updated {updated} lead statuses to 'Queued' in sheet")
    else:
        print("Failed to push leads to Instantly.")


if __name__ == "__main__":
    main()
