#!/usr/bin/env python3
"""
Sync Replies Script
===================
Syncs reply data from Instantly.ai to Google Sheets CRM.

Usage:
    python scripts/sync_replies.py

Environment variables:
    INSTANTLY_API_KEY - Instantly API key (required)
    GOOGLE_CREDENTIALS_FILE - Path to Google credentials JSON
    SPREADSHEET_ID - Google Sheets spreadsheet ID

Can be scheduled with cron:
    # Run every hour
    0 * * * * cd /path/to/lead-automation && python scripts/sync_replies.py >> logs/sync.log 2>&1
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

# Load environment variables
load_dotenv(project_root / ".env")

from src.outreach.sync_replies import sync_replies_from_instantly


def main():
    print(f"Starting reply sync at {__import__('datetime').datetime.now()}")
    print("-" * 50)

    try:
        results = sync_replies_from_instantly()

        print(f"Campaigns checked: {results['campaigns_checked']}")
        print(f"Replies found: {results['replies_found']}")
        print(f"CRM updated: {results['crm_updated']}")
        print(f"Already synced: {results['already_synced']}")
        print(f"Not in CRM: {results['not_in_crm']}")

        if results['errors']:
            print(f"\nErrors ({len(results['errors'])}):")
            for error in results['errors']:
                print(f"  - {error}")

        print("-" * 50)
        print("Sync complete!")

        # Exit with error code if there were errors
        sys.exit(1 if results['errors'] else 0)

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
