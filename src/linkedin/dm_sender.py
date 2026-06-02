"""
LinkedIn Outbound — DM Sender
================================
Sends personalized LinkedIn DMs to accepted connections.
Uses Claude API for personalization, browser automation for sending.

DM sequence:
- MSG 1: Immediate after acceptance — direct pitch referencing their hiring signal
- MSG 2: 5 days after MSG 1 — follow-up with specific result
- MSG 3: 10 days after MSG 2 — graceful close
"""

import os
import sys
import argparse
import requests
from pathlib import Path
from datetime import datetime
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.linkedin.config import (
    DM_TEMPLATES,
    SAFETY_LIMITS,
    get_next_dm1_variant,
    get_dm1_template,
)
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


def personalize_dm(
    api_key: str,
    dm_number: int,
    prospect: dict,
    variant: str = "",
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Generate a personalized DM using Claude API.

    Args:
        api_key: Anthropic API key
        dm_number: 1, 2, or 3
        prospect: Prospect data from CRM
        variant: For DM 1 only — "A", "B", or "C"
        model: Claude model to use

    Returns:
        Personalized DM text
    """
    # DM 1 uses variant-specific templates (A/B/C)
    if dm_number == 1 and variant:
        template = get_dm1_template(variant)
    else:
        template_key = f"dm{dm_number}"
        template = DM_TEMPLATES.get(template_key)

    if not template:
        logger.error("Unknown DM template", dm_number=dm_number, variant=variant)
        return ""

    # Extract first name
    contact_name = prospect.get("contact_name", "")
    first_name = contact_name.split()[0] if contact_name else "there"

    # Build the user prompt with prospect data
    user_prompt = template["user_prompt"].format(
        first_name=first_name,
        title=prospect.get("title", ""),
        company=prospect.get("company", ""),
        industry=prospect.get("industry", "B2B"),
        job_hiring=prospect.get("job_hiring", "SDR/BDR"),
        country=prospect.get("country", ""),
        description_snippet=prospect.get("description_snippet", ""),
    )

    # Call Claude API
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 300,
                "system": template["system_prompt"],
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract text from response
        content = data.get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "")
            # Clean up — remove quotes if Claude wrapped it
            text = text.strip().strip('"').strip("'")
            return text

    except Exception as e:
        logger.error("Claude personalization failed",
                     dm_number=dm_number,
                     company=prospect.get("company"),
                     error=str(e))

    return ""


def get_browser_instructions(messages: list[dict]) -> str:
    """Generate browser instructions for sending DMs.

    Args:
        messages: List of dicts with prospect info + personalized DM text

    Returns:
        Prompt for Claude in Chrome
    """
    instructions = f"""Send LinkedIn direct messages to {len(messages)} people.

For each person below:
1. Navigate to their LinkedIn profile
2. Click the "Message" button to open the messaging window
3. Type the EXACT message provided (do not modify it)
4. Click Send
5. Wait {SAFETY_LIMITS['delay_between_actions_min']}-{SAFETY_LIMITS['delay_between_actions_max']} seconds before the next one
6. If you cannot message them (not connected, messaging disabled), skip and report

Messages to send:
"""
    for i, msg in enumerate(messages, 1):
        instructions += f"\n{'='*50}"
        instructions += f"\n{i}. {msg['contact_name']} @ {msg['company']}"
        instructions += f"\n   Profile: {msg['linkedin_url']}"
        instructions += f"\n   CRM ID: {msg['id']}"
        variant_info = f" (variant {msg.get('dm_variant', '')})" if msg.get("dm_variant") else ""
        instructions += f"\n   DM #{msg['dm_number']}{variant_info}"
        instructions += f"\n   MESSAGE:"
        instructions += f"\n   {msg['dm_text']}"
        instructions += f"\n{'='*50}\n"

    instructions += """
After each message, report:
- Person's name and CRM ID
- Whether the message was sent successfully or skipped
- Any errors encountered

STOP if you hit any LinkedIn messaging limit warning.
"""
    return instructions


def run(dry_run: bool = False):
    """Main entry point — find DM-ready prospects, personalize, generate instructions."""
    config = load_config()
    anthropic_key = config["api_keys"]["anthropic"]

    crm = LinkedInSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
    )

    # Collect DM-ready prospects for all 3 message stages
    all_messages = []

    # Get current variant counts for round-robin assignment
    variant_counts = crm.get_variant_counts()
    print(f"\n  DM 1 variant counts so far: {variant_counts}")

    for dm_num in [1, 2, 3]:
        ready = crm.get_dm_ready(dm_num)
        if ready:
            print(f"\n  DM {dm_num} ready: {len(ready)} prospects")

        for prospect in ready:
            if len(all_messages) >= SAFETY_LIMITS["max_dms_per_day"]:
                break

            # Pick variant for DM 1 (round-robin A/B/C)
            variant = ""
            if dm_num == 1:
                variant = get_next_dm1_variant(variant_counts)
                variant_counts[variant] = variant_counts.get(variant, 0) + 1

            if dry_run:
                variant_label = f" [variant {variant}]" if variant else ""
                all_messages.append({
                    **prospect,
                    "dm_number": dm_num,
                    "dm_variant": variant,
                    "dm_text": f"[DRY RUN — would personalize DM {dm_num}{variant_label}]",
                })
                continue

            # Personalize with Claude
            dm_text = personalize_dm(anthropic_key, dm_num, prospect, variant=variant)
            if not dm_text:
                logger.warning("Empty DM text", company=prospect.get("company"), dm=dm_num)
                # Roll back variant count if personalization failed
                if variant:
                    variant_counts[variant] -= 1
                continue

            all_messages.append({
                **prospect,
                "dm_number": dm_num,
                "dm_variant": variant,
                "dm_text": dm_text,
            })

            logger.info("DM personalized",
                        company=prospect.get("company"),
                        dm=dm_num,
                        variant=variant or "N/A",
                        length=len(dm_text))

    print(f"\n--- DM Sender Summary ---")
    print(f"  Total DMs to send: {len(all_messages)}")
    for dm_num in [1, 2, 3]:
        count = sum(1 for m in all_messages if m["dm_number"] == dm_num)
        if count:
            if dm_num == 1:
                variants_this_run = {}
                for m in all_messages:
                    if m["dm_number"] == 1:
                        v = m.get("dm_variant", "?")
                        variants_this_run[v] = variants_this_run.get(v, 0) + 1
                print(f"  DM 1: {count} (variants: {variants_this_run})")
            else:
                print(f"  DM {dm_num}: {count}")

    # Print variant performance stats
    variant_stats = crm.get_variant_reply_rates()
    print(f"\n--- Variant Performance ---")
    for v, s in variant_stats.items():
        rate_pct = f"{s['rate']*100:.0f}%" if s['sent'] > 0 else "N/A"
        print(f"  Variant {v}: {s['sent']} sent, {s['replied']} replied ({rate_pct})")

    if not all_messages:
        print("  No DMs to send. Exiting.")
        return None

    if dry_run:
        print(f"\n--- DRY RUN ---")
        for msg in all_messages:
            print(f"  DM {msg['dm_number']} → {msg.get('contact_name', '?')} @ {msg.get('company', '?')}")
        return all_messages

    # Print the personalized messages for review
    print(f"\n--- Personalized DMs ---")
    for msg in all_messages:
        print(f"\n  DM {msg['dm_number']} → {msg.get('contact_name', '?')} @ {msg.get('company', '?')}")
        print(f"  {msg['dm_text']}")

    # Generate browser instructions
    instructions = get_browser_instructions(all_messages)
    print(f"\n--- Browser Instructions ---")
    print(instructions)

    return all_messages


def main():
    parser = argparse.ArgumentParser(description="LinkedIn DM Sender")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
