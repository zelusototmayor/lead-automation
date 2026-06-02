"""
LinkedIn Outbound — Reply Monitor
====================================
Checks LinkedIn messaging inbox for replies from prospects.
Updates CRM status and sends ntfy push notification to Jose.
"""

import os
import sys
import argparse
import requests
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


def send_ntfy_notification(topic: str, title: str, message: str):
    """Send push notification via ntfy.sh."""
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "speech_balloon,linkedin",
            },
            timeout=10,
        )
        logger.info("ntfy notification sent", topic=topic, title=title)
    except Exception as e:
        logger.error("ntfy notification failed", error=str(e))


def get_browser_instructions(active_prospects: list[dict]) -> str:
    """Generate browser instructions for checking LinkedIn inbox.

    Returns a prompt for Claude in Chrome.
    """
    # Build lookup of active prospect names
    prospect_names = []
    for p in active_prospects:
        name = p.get("contact_name", "")
        company = p.get("company", "")
        lead_id = p.get("id", "")
        status = p.get("status", "")
        prospect_names.append(f"  - {name} ({company}) [{status}] [ID: {lead_id}]")

    instructions = f"""Check LinkedIn messaging inbox for new replies from prospects.

1. Go to https://www.linkedin.com/messaging/
2. Look at recent conversations (last 48 hours)
3. Check if any of the following people have sent you a new message:

{chr(10).join(prospect_names)}

4. For each reply found:
   - Report the person's name and CRM ID from the list above
   - Copy the FIRST 200 characters of their reply message
   - Note if the reply seems positive (interested), neutral (question), or negative (not interested)

5. Also check for replies from people NOT in the list — they might be from
   earlier LinkedIn activity (report those separately).

6. Do NOT send any messages — this is read-only monitoring.
"""
    return instructions


def run(dry_run: bool = False):
    """Main entry point — generate instructions for reply monitoring."""
    config = load_config()
    ntfy_topic = config.get("monitoring", {}).get("ntfy_topic", "")

    crm = LinkedInSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
    )

    # Get all prospects who could have replied (DM 1-3 sent, not yet replied)
    active = []
    for status in ("DM 1", "DM 2", "DM 3", "Connected"):
        prospects = crm.get_prospects_by_status(status, limit=200)
        active.extend(prospects)

    print(f"\n--- Reply Monitor ---")
    print(f"  Active prospects to check: {len(active)}")

    stats = crm.get_stats()
    print(f"  Already replied: {stats.get('replied', 0)}")

    if not active:
        print("  No active DM conversations to monitor. Exiting.")
        return None

    if dry_run:
        print(f"\n--- DRY RUN ---")
        print(f"  Would check inbox for replies from {len(active)} prospects:")
        for p in active[:10]:
            print(f"    {p.get('contact_name', '?'):30s} | {p.get('company', ''):25s} | {p.get('status', '')}")
        if len(active) > 10:
            print(f"    ... and {len(active) - 10} more")
        return active

    # Generate browser instructions
    instructions = get_browser_instructions(active)
    print(f"\n--- Browser Instructions ---")
    print(instructions)

    return active


def handle_reply(
    config: dict,
    crm: LinkedInSheetsCRM,
    lead_id: str,
    reply_text: str,
    prospect_name: str = "",
    company: str = "",
):
    """Process a detected reply — update CRM and send notification.

    Called by the Cowork scheduled task after browser automation detects a reply.
    """
    crm.mark_replied(lead_id, reply_text)

    ntfy_topic = config.get("monitoring", {}).get("ntfy_topic", "")
    if ntfy_topic:
        send_ntfy_notification(
            topic=ntfy_topic,
            title=f"LinkedIn Reply: {prospect_name}",
            message=f"{prospect_name} @ {company} replied:\n{reply_text[:200]}",
        )

    logger.info("Reply processed",
                lead_id=lead_id,
                name=prospect_name,
                company=company,
                reply_preview=reply_text[:100])


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Reply Monitor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
