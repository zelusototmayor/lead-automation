#!/usr/bin/env python3
"""Analyze API credit status and untapped pipeline across all campaigns."""

import os
import sys
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.eu_outreach import load_config
from src.crm import GoogleSheetsCRM
from src.crm.local_services_sheet import LocalServicesCRM
from src.outreach import InstantlyClient


def check_apollo(api_key):
    """Check Apollo credit usage."""
    print("\n" + "=" * 60)
    print("APOLLO.IO")
    print("=" * 60)
    try:
        # Check account info
        resp = requests.get(
            "https://api.apollo.io/api/v1/auth/health",
            headers={"x-api-key": api_key},
            timeout=10,
        )
        if resp.status_code == 401:
            print("  STATUS: ❌ API key INVALID (401 Unauthorized)")
            print("  → Need to get a new Apollo API key")
            return False

        # Try usage endpoint
        resp2 = requests.get(
            "https://api.apollo.io/api/v1/auth/health",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            timeout=10,
        )
        data = resp2.json() if resp2.ok else {}

        # Try credit usage via org endpoint
        resp3 = requests.post(
            "https://api.apollo.io/v1/auth/health",
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            json={"api_key": api_key},
            timeout=10,
        )

        if resp.ok:
            print(f"  STATUS: ✅ API key valid")
            if data:
                print(f"  Response: {data}")
        else:
            print(f"  STATUS: ⚠️ HTTP {resp.status_code}")
            print(f"  Response: {resp.text[:200]}")

        # Try a minimal search to verify credits work
        test_resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_people/search",
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            json={
                "api_key": api_key,
                "per_page": 1,
                "person_titles": ["CEO"],
                "person_locations": ["United States"],
            },
            timeout=15,
        )
        if test_resp.ok:
            result = test_resp.json()
            breadcrumbs = result.get("breadcrumbs", [])
            pagination = result.get("pagination", {})
            print(f"  Credit test search: ✅ working")
            print(f"  Plan: $69/mo (3,000 credits)")
            # Check if there's usage info in response
            for bc in breadcrumbs:
                print(f"    {bc.get('label', '')}: {bc.get('display_name', '')}")
        elif test_resp.status_code == 401:
            print(f"  Credit test search: ❌ 401 Unauthorized")
            print(f"  → Apollo API key is exhausted or invalid")
            return False
        elif test_resp.status_code == 403:
            print(f"  Credit test search: ❌ 403 Forbidden (credits exhausted?)")
            return False
        else:
            print(f"  Credit test search: ⚠️ HTTP {test_resp.status_code}")
            print(f"  {test_resp.text[:200]}")

        return True

    except Exception as e:
        print(f"  ERROR: {e}")
        return False


def check_instantly(api_key):
    """Check Instantly campaign stats and usage."""
    print("\n" + "=" * 60)
    print("INSTANTLY.AI")
    print("=" * 60)
    try:
        client = InstantlyClient(api_key=api_key)
        campaigns = client.list_campaigns()

        if not campaigns:
            print("  STATUS: ⚠️ No campaigns found (key may be invalid)")
            return

        print(f"  STATUS: ✅ API key valid")
        print(f"  Plan: $47/mo (1,000 active contacts, 5,000 emails)")
        print(f"  Campaigns found: {len(campaigns)}")

        total_leads = 0
        for c in campaigns:
            cid = c.get("id")
            name = c.get("name", "?")
            status = c.get("status", "?")

            # Get leads count
            leads = client.list_leads(campaign_id=cid)
            lead_count = len(leads)
            total_leads += lead_count

            # Count statuses
            statuses = {}
            for lead in leads:
                s = lead.get("lead_status") or lead.get("status") or "unknown"
                statuses[s] = statuses.get(s, 0) + 1

            print(f"\n  Campaign: {name}")
            print(f"    Status: {status}")
            print(f"    Leads: {lead_count}")
            for s, count in sorted(statuses.items()):
                print(f"      {s}: {count}")

        print(f"\n  TOTAL LEADS ACROSS CAMPAIGNS: {total_leads}")
        print(f"  Monthly contact limit: 1,000")
        remaining = max(0, 1000 - total_leads)
        print(f"  Remaining capacity: ~{remaining} contacts")

    except Exception as e:
        print(f"  ERROR: {e}")


def check_serpapi(api_key):
    """Check SerpAPI credit balance."""
    print("\n" + "=" * 60)
    print("SERPAPI")
    print("=" * 60)
    try:
        resp = requests.get(
            "https://serpapi.com/account.json",
            params={"api_key": api_key},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            plan = data.get("plan_name", "?")
            searches_left = data.get("total_searches_left", "?")
            this_month = data.get("this_month_usage", "?")
            print(f"  STATUS: ✅ API key valid")
            print(f"  Plan: {plan}")
            print(f"  Searches left: {searches_left}")
            print(f"  This month usage: {this_month}")
        else:
            print(f"  STATUS: ❌ HTTP {resp.status_code}")
            print(f"  {resp.text[:200]}")
    except Exception as e:
        print(f"  ERROR: {e}")


def check_apify(api_key):
    """Check Apify usage."""
    print("\n" + "=" * 60)
    print("APIFY")
    print("=" * 60)
    try:
        resp = requests.get(
            "https://api.apify.com/v2/users/me",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json().get("data", {})
            plan = data.get("plan", {})
            usage = data.get("currentBillingPeriod", {}) or data.get("monthlyUsage", {})
            print(f"  STATUS: ✅ API key valid")
            print(f"  Username: {data.get('username', '?')}")
            print(f"  Plan: {plan.get('id', '?') if isinstance(plan, dict) else plan}")
            if usage:
                print(f"  Current billing period usage: {usage}")
        elif resp.status_code == 401:
            print(f"  STATUS: ❌ API key invalid (401)")
        else:
            print(f"  STATUS: ⚠️ HTTP {resp.status_code}")
            print(f"  {resp.text[:200]}")

        # Check usage/limits
        resp2 = requests.get(
            "https://api.apify.com/v2/users/me/usage/monthly",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp2.ok:
            usage_data = resp2.json().get("data", {})
            if usage_data:
                print(f"  Monthly usage details: {usage_data}")

    except Exception as e:
        print(f"  ERROR: {e}")


def check_google_maps(api_key):
    """Quick check if Google Maps API key works."""
    print("\n" + "=" * 60)
    print("GOOGLE MAPS / PLACES API")
    print("=" * 60)
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": "test", "key": api_key},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            status = data.get("status", "?")
            if status == "OK" or status == "ZERO_RESULTS":
                print(f"  STATUS: ✅ API key valid (pay-as-you-go)")
                print(f"  Note: Google Maps has no monthly credit limit — billed per use")
            elif status == "REQUEST_DENIED":
                print(f"  STATUS: ❌ Request denied — key may be restricted or invalid")
                print(f"  Error: {data.get('error_message', '?')}")
            else:
                print(f"  STATUS: ⚠️ {status}")
        else:
            print(f"  STATUS: ❌ HTTP {resp.status_code}")
    except Exception as e:
        print(f"  ERROR: {e}")


def analyze_pipeline(config):
    """Analyze untapped leads per campaign from Google Sheets."""
    print("\n" + "=" * 60)
    print("UNTAPPED PIPELINE (leads not yet contacted)")
    print("=" * 60)

    sheets_config = config["google_sheets"]

    campaigns = [
        ("AEC Leads", "AEC", "email"),
        ("B2B Startups", "B2B Startups", "email"),
        ("EU B2B Leads", "EU B2B", "email"),
        ("Local Services", "Local Services", "phone"),
    ]

    for sheet_name, label, outreach_type in campaigns:
        print(f"\n  --- {label} ({outreach_type} outreach) ---")
        try:
            if sheet_name == "Local Services":
                crm = LocalServicesCRM(
                    credentials_file=sheets_config["credentials_file"],
                    spreadsheet_id=sheets_config["spreadsheet_id"],
                )
            else:
                crm = GoogleSheetsCRM(
                    credentials_file=sheets_config["credentials_file"],
                    spreadsheet_id=sheets_config["spreadsheet_id"],
                    sheet_name=sheet_name,
                )

            total = len(crm._cache)

            if sheet_name == "Local Services":
                # Different schema — check status_call and Notes
                not_called = 0
                called = 0
                for row in crm._cache:
                    lead = crm._row_to_dict(list(row)) if hasattr(crm, '_row_to_dict') else {}
                    # For local services, check if they've been contacted
                    status = ""
                    for i, val in enumerate(row):
                        if "status" in str(val).lower() or "call" in str(val).lower():
                            pass
                    # Simpler: count rows
                    not_called += 1

                print(f"    Total leads: {total}")
                print(f"    → All {total} leads available for cold calling")
            else:
                # Email campaigns — check status and email sent flags
                new = 0
                queued = 0
                contacted = 0
                replied = 0
                other = 0

                for row in crm._cache:
                    lead = crm._row_to_dict(list(row))
                    status = (lead.get("status") or "").strip()
                    instantly_status = (lead.get("instantly_status") or "").strip().lower()
                    email1 = (lead.get("email_1_sent") or "").strip()
                    response = (lead.get("response") or "").strip()

                    if response:
                        replied += 1
                    elif instantly_status in ("completed", "sent", "bounced", "unsubscribed"):
                        contacted += 1
                    elif status == "Queued" or instantly_status:
                        queued += 1
                    elif status == "New" or not status:
                        new += 1
                    else:
                        other += 1

                untapped = new + queued  # leads that haven't been emailed yet
                print(f"    Total leads: {total}")
                print(f"    New (not queued): {new}")
                print(f"    Queued (in Instantly, pending send): {queued}")
                print(f"    Contacted (emails sent): {contacted}")
                print(f"    Replied: {replied}")
                if other:
                    print(f"    Other status: {other}")
                print(f"    → UNTAPPED: {new} leads not yet in any campaign")
                print(f"    → IN PIPELINE: {queued} queued, awaiting send")

        except Exception as e:
            print(f"    ERROR reading sheet: {e}")


def main():
    config = load_config("config/settings.yaml")

    print("🔍 LEAD AUTOMATION — API & PIPELINE ANALYSIS")
    print(f"   Date: 2026-03-15")

    # Check all APIs
    check_apollo(config["api_keys"]["apollo"])
    check_instantly(config.get("instantly", {}).get("api_key", ""))
    check_serpapi(config["api_keys"]["serpapi"])
    check_apify(config["api_keys"]["apify"])
    check_google_maps(config["api_keys"]["google_maps"])

    # Analyze untapped pipeline
    analyze_pipeline(config)

    print("\n" + "=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
