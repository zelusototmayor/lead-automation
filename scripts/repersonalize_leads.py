#!/usr/bin/env python3
"""
Re-personalize leads with generic/broken personalization text.
Fetches leads from Instantly, re-runs Claude personalization, updates via PATCH.

Usage:
    python -m scripts.repersonalize_leads --dry-run
    python -m scripts.repersonalize_leads --execute
"""

import os
import sys
import time
import argparse
import yaml
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.outreach.instantly_client import InstantlyClient
from src.outreach.personalize import EmailPersonalizer

load_dotenv()

# Phrases that indicate broken/generic personalization
BAD_PHRASES = [
    "I came across",
    "I was impressed",
    "caught my attention",
    "I noticed your company",
    "As a company that",
    "It's clear that",
    "In today's competitive landscape",
    "your work in .",  # missing industry
]

# Campaigns to fix: (original campaign ID, copy campaign ID)
CAMPAIGNS = {
    "AEC Business Development": {
        "original_id": "2a38b20e-8460-4af9-bbb9-5647b85194d8",
        "copy_id": "6adb10ba-1439-4046-80f9-900127633995",
    },
    "B2B Startups Outbound": {
        "original_id": "7e876694-3796-44d9-8a7e-ed00f2b92ca1",
        "copy_id": "2899d58a-1d42-4fa8-9a73-699b3a7e51f3",
    },
}


def has_bad_personalization(lead: dict) -> bool:
    """Check if a lead has generic/broken personalization."""
    payload = lead.get("payload", {})
    opener = payload.get("personalized_opener", "")
    pain = payload.get("specific_pain_point", "")
    text = f"{opener} {pain}"
    return any(phrase in text for phrase in BAD_PHRASES)


def build_lead_context(lead: dict) -> dict:
    """Build lead dict for the personalizer from Instantly lead data."""
    payload = lead.get("payload", {})
    return {
        "company": lead.get("company_name", ""),
        "contact_name": f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip(),
        "title": payload.get("title", ""),
        "industry": payload.get("industry", ""),
        "employee_count": payload.get("employee_count", ""),
        "city": payload.get("city", ""),
        "country": payload.get("country", ""),
        "website": lead.get("website", ""),
        "description": payload.get("description", ""),
        "keywords": payload.get("keywords", []),
        "technologies": payload.get("technologies", []),
        "signal_context": payload.get("signal_context", ""),
    }


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    api_key = os.getenv("INSTANTLY_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key or not anthropic_key:
        print("ERROR: INSTANTLY_API_KEY and ANTHROPIC_API_KEY must be set")
        sys.exit(1)

    client = InstantlyClient(api_key)
    personalizer = EmailPersonalizer(api_key=anthropic_key)

    # Load sender info and templates
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
    with open(os.path.join(config_path, "settings.yaml")) as f:
        config = yaml.safe_load(f)
    with open(os.path.join(config_path, "email_templates.yaml")) as f:
        templates = yaml.safe_load(f)

    sender_info = {
        "bio": config.get("personalization", {}).get("sender_bio", ""),
        "value_proposition": config.get("personalization", {}).get("value_proposition", ""),
        "aec_verticals": templates.get("aec_verticals"),
    }
    template = templates["sequences"]["default"]["emails"][0]["body_template"]

    for campaign_name, ids in CAMPAIGNS.items():
        print(f"\n{'='*60}")
        print(f"[CAMPAIGN] {campaign_name}")
        print(f"{'='*60}")

        # Collect bad leads from BOTH original and copy campaigns
        bad_leads = []
        for label, cid in [("original", ids["original_id"]), ("copy", ids["copy_id"])]:
            leads = client.list_leads(campaign_id=cid)
            bad = [l for l in leads if has_bad_personalization(l)]
            print(f"  {label}: {len(leads)} total, {len(bad)} with bad personalization")
            for l in bad:
                l["_source_campaign"] = cid
                l["_source_label"] = label
            bad_leads.extend(bad)

        # Deduplicate by email (prefer copy campaign version)
        seen_emails = {}
        for l in bad_leads:
            email = l.get("email")
            if email not in seen_emails or l["_source_label"] == "copy":
                seen_emails[email] = l
        bad_leads = list(seen_emails.values())

        print(f"  → {len(bad_leads)} unique leads to re-personalize")

        if args.dry_run:
            for l in bad_leads[:3]:
                p = l.get("payload", {})
                print(f"    {l.get('email')}: \"{p.get('personalized_opener', '')[:60]}...\"")
            if len(bad_leads) > 3:
                print(f"    ... and {len(bad_leads) - 3} more")
            continue

        # Re-personalize and update
        success = 0
        failed = 0
        for i, lead in enumerate(bad_leads):
            lead_context = build_lead_context(lead)

            result = personalizer.personalize_email(
                lead=lead_context,
                template=template,
                sender_info=sender_info,
            )

            if not result:
                print(f"  [{i+1}/{len(bad_leads)}] FAILED (Claude error): {lead.get('email')}")
                failed += 1
                continue

            # Update in Instantly via PATCH
            custom_vars = {
                "personalized_opener": result.get("personalized_opener", ""),
                "specific_pain_point": result.get("specific_pain_point", ""),
                "industry_specific_insight": result.get("industry_specific_insight", ""),
                "suggested_subject": result.get("suggested_subject", ""),
            }

            lead_id = lead.get("id")
            update_result = client.update_lead(lead_id, {"custom_variables": custom_vars})

            if update_result:
                success += 1
                print(f"  [{i+1}/{len(bad_leads)}] OK: {lead.get('email')} → \"{result.get('personalized_opener', '')[:50]}...\"")
            else:
                failed += 1
                print(f"  [{i+1}/{len(bad_leads)}] PATCH FAILED: {lead.get('email')}")

            # Small delay to avoid rate limits (Claude + Instantly)
            time.sleep(0.5)

        print(f"\n  Done: {success} updated, {failed} failed")

    if args.dry_run:
        print("\nRun with --execute to apply fixes.")


if __name__ == "__main__":
    main()
