"""
Create v2 Google Sheets Tabs
=============================
One-shot script: instantiates GoogleSheetsCRM for each v2 tab, which
auto-creates the tab with CRM_HEADERS if it doesn't already exist.

Old tabs (AEC Leads, B2B Startups, EU B2B Leads) are never touched.

Usage:
    python scripts/create_v2_tabs.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_dotenv():
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def main():
    _load_dotenv()

    from src.crm.sheets import GoogleSheetsCRM

    SPREADSHEET_ID = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
    CREDS = "config/google_credentials.json"

    tabs = [
        "AEC Leads v2",
        "B2B Startups v2",
        "EU B2B Leads v2",
        "Logistics Quote v2",
    ]

    for tab in tabs:
        print(f"Creating tab: {tab} ...", end=" ", flush=True)
        try:
            GoogleSheetsCRM(
                credentials_file=CREDS,
                spreadsheet_id=SPREADSHEET_ID,
                sheet_name=tab,
            )
            print("OK")
        except Exception as e:
            print(f"ERROR: {e}")

    print("\nDone. Old tabs left untouched.")


if __name__ == "__main__":
    main()
