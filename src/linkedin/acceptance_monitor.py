"""
LinkedIn Outbound — Acceptance Monitor
========================================
Checks for accepted connection requests from TWO sources:
1. LinkedIn notifications page (browser automation)
2. Manual "Connected?" toggle in Google Sheets CRM

Updates prospect status to "Connected" and queues for DM 1.
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import datetime
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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


def check_manual_toggles(crm: LinkedInSheetsCRM) -> list[dict]:
    """Check for manually toggled connections in the CRM.

    Jose may see LinkedIn notifications before the automation does
    and toggle the "Connected? (manual)" checkbox in Sheets.
    """
    toggled = crm.get_manual_toggles()
    updated = []

    for prospect in toggled:
        lead_id = prospect.get("id")
        if not lead_id:
            continue

        success = crm.mark_connected(lead_id)
        if success:
            updated.append(prospect)
            logger.info("Manual toggle → Connected",
                        name=prospect.get("contact_name"),
                        company=prospect.get("company"))

    return updated


def get_browser_instructions(pending: list[dict]) -> str:
    """Generate browser instructions for checking LinkedIn notifications.

    Returns a prompt for Claude in Chrome to check for accepted connections
    and match them to pending prospects.
    """
    # Build a lookup of pending prospect names and companies
    pending_names = []
    for p in pending:
        name = p.get("contact_name", "")
        company = p.get("company", "")
        lead_id = p.get("id", "")
        pending_names.append(f"  - {name} ({company}) [ID: {lead_id}]")

    instructions = f"""Check LinkedIn for accepted connection requests.

1. Go to https://www.linkedin.com/mynetwork/invitation-manager/sent/
   This shows all your pending and recently accepted connection invitations.

2. Look for invitations that show "Accepted" status.
   Also check https://www.linkedin.com/notifications/ for "accepted your invitation" notifications.

3. For each accepted connection, check if they match any of these pending prospects:

{chr(10).join(pending_names)}

4. For each match found, report:
   - The person's name
   - Their CRM ID from the list above
   - Confirmation that the connection was accepted

5. If you find accepted connections that are NOT in the pending list, still report
   them (they might be from manual LinkedIn use).

Note: Some notifications may have already disappeared if Jose saw them first.
That's OK — manual toggles are handled separately.
"""
    return instructions


def run(dry_run: bool = False):
    """Main entry point — check both CRM toggles and generate browser instructions."""
    config = load_config()

    crm = LinkedInSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
    )

    stats = crm.get_stats()
    print(f"\n--- Acceptance Monitor ---")
    print(f"  Pending connections:  {stats.get('request_sent', 0)}")
    print(f"  Already connected:    {stats.get('connected', 0)}")

    # Source 1: Manual CRM toggles
    manual_updates = check_manual_toggles(crm)
    print(f"\n  Manual toggles found: {len(manual_updates)}")
    for p in manual_updates:
        print(f"    + {p.get('contact_name', '?')} @ {p.get('company', '?')}")

    # Source 2: LinkedIn notifications (browser)
    pending = crm.get_pending_connections()
    print(f"\n  Pending connections to check on LinkedIn: {len(pending)}")

    if not pending and not manual_updates:
        print("  Nothing to check. Exiting.")
        return None

    if dry_run:
        print(f"\n--- DRY RUN ---")
        print(f"  Would check LinkedIn for {len(pending)} pending connections")
        return pending

    if pending:
        instructions = get_browser_instructions(pending)
        print(f"\n--- Browser Instructions ---")
        print(instructions)

    newly_connected = len(manual_updates)
    print(f"\n  Total newly connected this run: {newly_connected}")
    return pending


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Acceptance Monitor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
