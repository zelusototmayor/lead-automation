#!/usr/bin/env python3
"""
Instantly Campaign Copy Script
==============================
Creates "(copy)" versions of campaigns with only uncontacted leads.

Usage:
    python -m scripts.instantly_campaign_copy --dry-run   # Preview only
    python -m scripts.instantly_campaign_copy --execute    # Actually do it
"""

import os
import sys
import argparse
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.outreach.instantly_client import InstantlyClient

load_dotenv()

# Original campaign name → existing (copy) campaign ID (if already created)
CAMPAIGNS_TO_COPY = {
    "AEC Business Development": "6adb10ba-1439-4046-80f9-900127633995",       # "AEC Business Development (copy)"
    "B2B Startups Outbound":    "2899d58a-1d42-4fa8-9a73-699b3a7e51f3",       # "B2B Startups Outbound (copy)"
    "EU B2B - hiring sales":    None,                                          # needs to be created
}


def is_uncontacted(lead: dict) -> bool:
    """A lead is uncontacted if status_summary has no lastStep (no email sent)."""
    ss = lead.get("status_summary")
    if not ss or not ss.get("lastStep"):
        return True
    return False


def get_uncontacted_leads(client: InstantlyClient, campaign_id: str) -> tuple[list[dict], int, int]:
    """Fetch all leads and return (uncontacted_leads, contacted_count, total_count)."""
    all_leads = client.list_leads(campaign_id=campaign_id)
    total = len(all_leads)

    uncontacted = [l for l in all_leads if is_uncontacted(l)]
    contacted = total - len(uncontacted)

    return uncontacted, contacted, total


def dry_run(client: InstantlyClient):
    """Show what would happen without making changes."""
    print("\n=== DRY RUN — No changes will be made ===\n")

    campaigns = client.list_campaigns()
    print(f"Found {len(campaigns)} total campaigns in Instantly:\n")
    for c in campaigns:
        print(f"  - {c.get('name')} (id: {c.get('id')}, status: {c.get('status', '?')})")

    print()

    for name, existing_copy_id in CAMPAIGNS_TO_COPY.items():
        match = next((c for c in campaigns if c.get("name") == name), None)
        if not match:
            print(f"[SKIP] Campaign '{name}' not found!")
            continue

        campaign_id = match["id"]
        print(f"[CAMPAIGN] {name}")

        uncontacted, contacted, total = get_uncontacted_leads(client, campaign_id)
        print(f"  Total: {total} | Contacted: {contacted} | Uncontacted: {len(uncontacted)}")

        copy_name = f"{name} (copy)"
        if existing_copy_id:
            print(f"  → Will use existing '{copy_name}' (id: {existing_copy_id})")
        else:
            print(f"  → Will create new '{copy_name}'")
        print(f"  → Will add {len(uncontacted)} uncontacted leads")
        print()

    print("Run with --execute to perform the migration.")


def execute(client: InstantlyClient):
    """Create/use (copy) campaigns and add uncontacted leads."""
    print("\n=== EXECUTING — Migrating uncontacted leads ===\n")

    campaigns = client.list_campaigns()

    for name, existing_copy_id in CAMPAIGNS_TO_COPY.items():
        copy_name = f"{name} (copy)"

        match = next((c for c in campaigns if c.get("name") == name), None)
        if not match:
            print(f"[SKIP] Original campaign '{name}' not found!")
            continue

        campaign_id = match["id"]
        print(f"\n[CAMPAIGN] {name}")

        # Get uncontacted leads
        uncontacted, contacted, total = get_uncontacted_leads(client, campaign_id)
        print(f"  Total: {total} | Contacted: {contacted} | Uncontacted: {len(uncontacted)}")

        if not uncontacted:
            print(f"  No uncontacted leads — skipping.")
            continue

        # Get or create the copy campaign
        if existing_copy_id:
            new_id = existing_copy_id
            print(f"  Using existing '{copy_name}' (id: {new_id})")
        else:
            result = client.create_campaign(copy_name)
            if not result:
                print(f"  [ERROR] Failed to create '{copy_name}'")
                continue
            new_id = result.get("id")
            print(f"  Created '{copy_name}' (id: {new_id})")

        # Build lead list preserving custom variables from payload
        leads_to_add = []
        for lead in uncontacted:
            lead_data = {
                "email": lead.get("email"),
                "first_name": lead.get("first_name", ""),
                "last_name": lead.get("last_name", ""),
                "company_name": lead.get("company_name", ""),
                "website": lead.get("website", ""),
                "phone": lead.get("phone", ""),
            }
            # Carry over personalization custom variables from payload
            payload = lead.get("payload", {})
            for var in ("personalized_opener", "specific_pain_point",
                        "industry_specific_insight", "industry", "city",
                        "signal_hook", "suggested_subject"):
                if payload.get(var):
                    lead_data[var] = payload[var]

            leads_to_add.append(lead_data)

        print(f"  Adding {len(leads_to_add)} leads to '{copy_name}'...")
        added = client.add_leads_to_campaign(new_id, leads_to_add)
        success_count = len(added) if added else 0
        print(f"  Added {success_count}/{len(leads_to_add)} leads.")

    print("\nDone! Set up sequences and schedules for the (copy) campaigns in Instantly.")


def main():
    parser = argparse.ArgumentParser(description="Copy Instantly campaigns with uncontacted leads")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    group.add_argument("--execute", action="store_true", help="Create campaigns and migrate leads")
    args = parser.parse_args()

    api_key = os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        print("ERROR: INSTANTLY_API_KEY not set in environment")
        sys.exit(1)

    client = InstantlyClient(api_key)

    if args.dry_run:
        dry_run(client)
    else:
        execute(client)


if __name__ == "__main__":
    main()
