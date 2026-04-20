"""
Re-queue Polluted-Window Leads into v2 Tabs
============================================
During the polluted-inbox window (the weeks around the zelu@zelusottomayor.com
reputation damage), Instantly kept recording Email 1 as "sent" against leads
that in reality landed in spam or were throttled. Those leads effectively
never saw us. This script copies them into the fresh v2 tabs with a clean
status so they can be re-queued in the new campaigns.

The old tabs are NOT touched — per the user's explicit instruction:
"create fresh tabs but dont archive the old ones so i can have look and see
the diference".

Usage:
    python scripts/requeue_polluted_to_v2.py --dry-run       # Preview
    python scripts/requeue_polluted_to_v2.py                 # Execute
    python scripts/requeue_polluted_to_v2.py --campaign aec  # One campaign only
    python scripts/requeue_polluted_to_v2.py --since 2026-03-01 --until 2026-04-18

The window defaults to [POLLUTED_START, POLLUTED_END] below. Adjust if needed.
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import datetime
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm import GoogleSheetsCRM

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logger = structlog.get_logger()


# Default polluted window — adjust if your actual timeline differs.
# Covers the weeks leading up to the Monday 2026-04-20 restart.
POLLUTED_START = "2026-03-01"
POLLUTED_END   = "2026-04-18"

# Old tab → v2 tab mapping
TAB_MAP = {
    "aec":      ("AEC Leads",      "AEC Leads v2"),
    "startups": ("B2B Startups",   "B2B Startups v2"),
    "eu":       ("EU B2B Leads",   "EU B2B Leads v2"),
}


def _load_dotenv():
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def load_config():
    import yaml
    _load_dotenv()
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
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


def _in_window(date_str: str, start: str, end: str) -> bool:
    """Check whether a YYYY-MM-DD-ish date lands inside the polluted window."""
    if not date_str:
        return False
    try:
        # Handle both ISO dates and datetime strings
        d = datetime.fromisoformat(date_str.split("T")[0]).date()
    except Exception:
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except Exception:
            return False
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    return s <= d <= e


def _polluted(lead: dict, start: str, end: str) -> bool:
    """Polluted = email 1 was recorded sent during the bad window.

    We treat these leads as effectively un-contacted, so they're fair game
    to re-queue in the fresh v2 campaign without burning a fresh credit.
    """
    email_1_sent = str(lead.get("email_1_sent", "")).strip().lower()
    if email_1_sent not in ("true", "yes", "1", "sent"):
        return False

    # Use Last Contact or Date Added as the sent-proxy timestamp
    sent_at = lead.get("last_contact") or lead.get("date_added") or ""
    return _in_window(sent_at, start, end)


def _clean_for_requeue(lead: dict) -> dict:
    """Strip sent/engagement fields so the lead reads as fresh in v2."""
    cleaned = dict(lead)

    # Zero out engagement + send flags
    for key in [
        "email_1_sent", "email_2_sent", "email_3_sent", "email_4_sent",
        "opens", "clicks", "response", "instantly_status", "last_contact",
    ]:
        cleaned[key] = ""

    cleaned["status"] = "New"
    # Tag in notes so we can trace these rows in the v2 tab
    existing_notes = cleaned.get("notes", "") or ""
    tag = f"Requeued from polluted window ({datetime.now().strftime('%Y-%m-%d')})"
    cleaned["notes"] = f"{tag} | {existing_notes}" if existing_notes else tag

    return cleaned


def requeue_campaign(
    config: dict,
    old_tab: str,
    new_tab: str,
    start: str,
    end: str,
    dry_run: bool,
) -> dict:
    """Copy polluted leads from old_tab → new_tab."""
    print(f"\n[{old_tab} → {new_tab}]")

    old_crm = GoogleSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
        sheet_name=old_tab,
    )
    new_crm = GoogleSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
        sheet_name=new_tab,
    )

    try:
        all_leads = old_crm.get_leads_for_outreach(limit=10000)
    except Exception as e:
        print(f"  Could not read '{old_tab}' — skipping. Error: {e}")
        return {"scanned": 0, "polluted": 0, "requeued": 0, "skipped_dup": 0}

    # Existing emails in v2 so we don't double-write
    try:
        existing_v2_emails = new_crm.get_all_emails()
    except Exception:
        existing_v2_emails = set()

    scanned = len(all_leads)
    polluted = [l for l in all_leads if _polluted(l, start, end)]

    requeued = 0
    skipped_dup = 0
    for lead in polluted:
        email = (lead.get("email") or "").strip().lower()
        if not email:
            continue
        if email in existing_v2_emails:
            skipped_dup += 1
            continue

        cleaned = _clean_for_requeue(lead)

        if dry_run:
            print(f"  [dry] would requeue: {cleaned.get('company', '')} <{email}>")
            requeued += 1
            continue

        try:
            new_id = new_crm.add_lead(cleaned)
            if new_id:
                existing_v2_emails.add(email)
                requeued += 1
                print(f"  + {cleaned.get('company', ''):40s} <{email}>")
        except Exception as e:
            logger.error("Failed to write lead", email=email, error=str(e))

    print(
        f"  Scanned: {scanned}  Polluted: {len(polluted)}  "
        f"Requeued: {requeued}  Skipped (dup in v2): {skipped_dup}"
    )

    return {
        "scanned": scanned,
        "polluted": len(polluted),
        "requeued": requeued,
        "skipped_dup": skipped_dup,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Re-queue polluted-window leads from old tabs into v2 tabs"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without writing")
    parser.add_argument("--campaign", choices=list(TAB_MAP.keys()),
                        help="Run only one campaign (aec|startups|eu)")
    parser.add_argument("--since", default=POLLUTED_START,
                        help=f"Polluted window start (default {POLLUTED_START})")
    parser.add_argument("--until", default=POLLUTED_END,
                        help=f"Polluted window end (default {POLLUTED_END})")
    args = parser.parse_args()

    config = load_config()
    campaigns = [args.campaign] if args.campaign else list(TAB_MAP.keys())

    print(f"\n{'='*60}")
    print(f"Polluted window re-queue  [{args.since} → {args.until}]")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}")
    print(f"Campaigns: {', '.join(campaigns)}")
    print(f"{'='*60}")

    totals = {"scanned": 0, "polluted": 0, "requeued": 0, "skipped_dup": 0}
    for key in campaigns:
        old_tab, new_tab = TAB_MAP[key]
        r = requeue_campaign(config, old_tab, new_tab,
                             args.since, args.until, args.dry_run)
        for k in totals:
            totals[k] += r[k]

    print(f"\n{'='*60}")
    print(f"DONE — scanned:{totals['scanned']} "
          f"polluted:{totals['polluted']} "
          f"requeued:{totals['requeued']} "
          f"dup-in-v2:{totals['skipped_dup']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
