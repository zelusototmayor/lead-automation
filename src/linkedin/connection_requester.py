"""
LinkedIn Outbound — Connection Requester
==========================================
Sends connection requests (NO note) to prospects in the CRM.
Uses browser automation via Claude in Chrome.

Safety: 20/day max, 100/week, random delays, weekdays only.
"""

import os
import sys
import random
import time
import argparse
from pathlib import Path
from datetime import datetime
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.linkedin.config import SAFETY_LIMITS
from src.linkedin.sheets_crm import LinkedInSheetsCRM

logger = structlog.get_logger()


def load_config():
    import yaml
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

    config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    def replace_env_vars(obj):
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            return os.environ.get(obj[2:-1], "")
        elif isinstance(obj, dict):
            return {k: replace_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_env_vars(item) for item in obj]
        return obj

    return replace_env_vars(config)


def check_safety_limits(crm: LinkedInSheetsCRM) -> dict:
    """Check if we're within safety limits before sending."""
    now = datetime.now()
    weekday = now.weekday()
    hour = now.hour

    checks = {
        "is_weekday": weekday < 5,
        "is_business_hours": (
            SAFETY_LIMITS["business_hours_start"]
            <= hour
            < SAFETY_LIMITS["business_hours_end"]
        ),
        "week_count": crm.get_week_connection_count(),
        "week_limit": SAFETY_LIMITS["max_connections_per_week"],
        "daily_limit": SAFETY_LIMITS["max_connections_per_day"],
    }

    checks["week_ok"] = checks["week_count"] < checks["week_limit"]
    checks["can_send"] = (
        checks["is_weekday"]
        and checks["is_business_hours"]
        and checks["week_ok"]
    )

    remaining_today = min(
        checks["daily_limit"],
        checks["week_limit"] - checks["week_count"],
    )
    checks["remaining_today"] = max(0, remaining_today)

    return checks


def get_browser_instructions(prospects: list[dict]) -> str:
    """Generate browser instructions for sending connection requests.

    Returns a prompt for Claude in Chrome to execute.
    """
    instructions = f"""Send LinkedIn connection requests to {len(prospects)} people.
IMPORTANT: Do NOT include a connection note. Just send blank connection requests.

For each person below:
1. Navigate to their LinkedIn profile
2. Click the "Connect" button
3. If LinkedIn asks "How do you know [name]?", select "Other" and click "Connect" (NO note)
4. If there's a "Send without a note" option, use that
5. If the "Connect" button is not visible (already connected, pending, or "Follow" only):
   - Try clicking "More" → "Connect"
   - If still not available, skip this person
6. Wait {SAFETY_LIMITS['delay_between_actions_min']}-{SAFETY_LIMITS['delay_between_actions_max']} seconds between each request
7. If LinkedIn shows ANY warning about connection limits, STOP IMMEDIATELY

People to connect with:
"""
    for i, p in enumerate(prospects, 1):
        instructions += f"\n{i}. {p.get('contact_name', 'Unknown')} — {p.get('title', '')}"
        instructions += f"\n   Company: {p.get('company', '')}"
        instructions += f"\n   Profile: {p.get('linkedin_url', '')}"
        instructions += f"\n   ID (for CRM update): {p.get('id', '')}\n"

    instructions += f"""
After each successful connection request, report:
- The person's name
- Their CRM ID
- Whether the request was sent successfully or skipped

STOP if you see any LinkedIn restriction warning.
"""
    return instructions


def run(dry_run: bool = False):
    """Main entry point — check limits, get prospects, generate instructions."""
    config = load_config()

    crm = LinkedInSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
    )

    # Safety check
    checks = check_safety_limits(crm)
    print(f"\n--- Safety Checks ---")
    print(f"  Weekday:         {'YES' if checks['is_weekday'] else 'NO — skipping'}")
    print(f"  Business hours:  {'YES' if checks['is_business_hours'] else 'NO — skipping'}")
    print(f"  Week total:      {checks['week_count']}/{checks['week_limit']}")
    print(f"  Remaining today: {checks['remaining_today']}")

    if not checks["can_send"]:
        print("\nCannot send connection requests right now. Exiting.")
        return None

    # Get prospects
    limit = min(checks["remaining_today"], SAFETY_LIMITS["max_connections_per_day"])
    prospects = crm.get_new_prospects(limit=limit)
    print(f"\n  Prospects ready:  {len(prospects)} (will send up to {limit})")

    if not prospects:
        print("  No new prospects to connect with.")
        return None

    if dry_run:
        print(f"\n--- DRY RUN: Would send {len(prospects)} connection requests ---")
        for p in prospects:
            print(f"  {p.get('contact_name', '?'):30s} | {p.get('company', ''):30s} | {p.get('linkedin_url', '')}")
        return prospects

    # Generate browser instructions
    instructions = get_browser_instructions(prospects)
    print(f"\n--- Browser Instructions ---")
    print(instructions)

    return prospects


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Connection Requester")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
