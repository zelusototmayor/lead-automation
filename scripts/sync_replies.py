#!/usr/bin/env python3
"""
Sync Script
===========
Syncs lead data (emails, replies, status) from Instantly.ai to Google Sheets CRM.

Usage:
    python scripts/sync_replies.py

Environment variables:
    INSTANTLY_API_KEY - Instantly API key (required)
    GOOGLE_CREDENTIALS_FILE - Path to Google credentials JSON
    SPREADSHEET_ID - Google Sheets spreadsheet ID
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

load_dotenv(project_root / ".env")

from src.outreach.sync_instantly import sync_from_instantly


def main():
    print(f"Starting Instantly sync at {__import__('datetime').datetime.now()}")
    print("-" * 50)

    try:
        results = sync_from_instantly()

        print(f"Campaigns checked: {results['campaigns_checked']}")
        print(f"Leads checked: {results['leads_checked']}")
        print(f"CRM updated: {results['crm_updated']}")
        print(f"Replies found: {results['replies_found']}")
        print(f"Emails fetched: {results['emails_fetched']}")
        print(f"Not in CRM: {results['not_in_crm']}")

        if results['errors']:
            print(f"\nErrors ({len(results['errors'])}):")
            for error in results['errors']:
                print(f"  - {error}")

        print("-" * 50)
        print("Sync complete!")

        sys.exit(1 if results['errors'] else 0)

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
