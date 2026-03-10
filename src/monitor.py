"""
Pipeline Monitor & Alerting
=============================
Daily health check that runs after all pipelines. Tracks:
- Apollo credit usage vs monthly budget
- Instantly contacts added vs monthly cap
- Per-pipeline lead counts and daily pace
- Pipeline errors from logs

Sends push notifications via ntfy.sh (free, no signup).
Install the ntfy app on your phone → subscribe to your topic.

Usage:
    python src/monitor.py              # Run full check
    python src/monitor.py --status     # Quick status (no alerts)
"""

import os
import sys
import json
import yaml
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm import GoogleSheetsCRM
from src.outreach import InstantlyClient

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

STATE_FILE = Path(__file__).parent.parent / "logs" / "monitor_state.json"


def _load_dotenv():
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def load_config(config_path: str = "config/settings.yaml") -> dict:
    _load_dotenv()
    with open(config_path, "r") as f:
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


def _load_state() -> dict:
    """Load persistent state (tracks cumulative Apollo credits across runs)."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "month": datetime.now().strftime("%Y-%m"),
        "apollo_credits_used": 0,
        "leads_added": {"aec": 0, "startups": 0, "eu": 0},
        "instantly_contacts_added": 0,
        "last_run": None,
    }


def _save_state(state: dict):
    """Save state to disk."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _reset_if_new_month(state: dict) -> dict:
    """Reset counters if we've entered a new month."""
    current_month = datetime.now().strftime("%Y-%m")
    if state.get("month") != current_month:
        logger.info("New month detected — resetting counters",
                     old=state.get("month"), new=current_month)
        state = {
            "month": current_month,
            "apollo_credits_used": 0,
            "leads_added": {"aec": 0, "startups": 0, "eu": 0},
            "instantly_contacts_added": 0,
            "last_run": None,
        }
    return state


def _send_ntfy(topic: str, title: str, message: str, priority: str = "default", tags: str = ""):
    """Send push notification via ntfy.sh."""
    if not topic:
        logger.warning("No ntfy topic configured — skipping notification")
        return

    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags

    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Notification sent", topic=topic, title=title)
    except Exception as e:
        logger.error("Failed to send notification", error=str(e))


def _count_leads_in_sheet(config: dict, sheet_name: str) -> int:
    """Count total leads in a CRM sheet."""
    try:
        crm = GoogleSheetsCRM(
            credentials_file=config["google_sheets"]["credentials_file"],
            spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
            sheet_name=sheet_name,
        )
        stats = crm.get_stats()
        return stats.get("total_leads", 0)
    except Exception as e:
        logger.error("Failed to count leads", sheet=sheet_name, error=str(e))
        return -1


def _count_instantly_contacts(config: dict) -> dict:
    """Count total contacts across all Instantly campaigns."""
    try:
        client = InstantlyClient(
            api_key=config.get("instantly", {}).get("api_key", ""),
        )
        campaigns = client.list_campaigns()
        total = 0
        by_campaign = {}
        for c in campaigns:
            cid = c.get("id")
            name = c.get("name", "unknown")
            leads = client.list_leads(campaign_id=cid)
            count = len(leads)
            by_campaign[name] = count
            total += count
        return {"total": total, "by_campaign": by_campaign}
    except Exception as e:
        logger.error("Failed to count Instantly contacts", error=str(e))
        return {"total": -1, "by_campaign": {}, "error": str(e)}


def run_monitor(config: dict, status_only: bool = False) -> dict:
    """Run the full monitoring check."""
    mon = config.get("monitoring", {})
    limits = mon.get("monthly_limits", {})
    warn_pct = mon.get("warn_at_percent", 80) / 100
    crit_pct = mon.get("critical_at_percent", 95) / 100
    ntfy_topic = mon.get("ntfy_topic", "")

    now = datetime.now()
    day_of_month = now.day
    days_in_month = 30  # approximate
    days_remaining = max(days_in_month - day_of_month, 1)
    month_progress = day_of_month / days_in_month

    state = _load_state()
    state = _reset_if_new_month(state)

    alerts = []  # (level, message)
    report = {}

    # --- 1. CRM Lead Counts ---
    sheets = {
        "AEC": config["google_sheets"].get("sheet_name", "AEC Leads"),
        "Startups": config.get("startups", {}).get("sheet_name", "B2B Startups"),
        "EU": config.get("eu_outreach", {}).get("sheet_name", "EU B2B Leads"),
        "Local Services": config.get("local_services", {}).get("sheet_name", "Local Services"),
    }

    lead_counts = {}
    for name, sheet in sheets.items():
        count = _count_leads_in_sheet(config, sheet)
        lead_counts[name] = count

    report["lead_counts"] = lead_counts

    # --- 2. Instantly Contacts ---
    instantly_limit = limits.get("instantly_contacts", 1000)
    instantly_data = _count_instantly_contacts(config)
    instantly_total = instantly_data["total"]

    report["instantly"] = {
        "total_contacts": instantly_total,
        "limit": instantly_limit,
        "by_campaign": instantly_data.get("by_campaign", {}),
    }

    if instantly_total >= 0:
        usage_pct = instantly_total / instantly_limit if instantly_limit else 0
        report["instantly"]["usage_percent"] = round(usage_pct * 100, 1)

        if usage_pct >= crit_pct:
            alerts.append(("critical", f"Instantly contacts at {usage_pct:.0%} ({instantly_total}/{instantly_limit}). Almost at cap!"))
        elif usage_pct >= warn_pct:
            alerts.append(("warning", f"Instantly contacts at {usage_pct:.0%} ({instantly_total}/{instantly_limit})."))

        # Pace check: are we burning through contacts too fast?
        if month_progress > 0:
            projected = instantly_total / month_progress
            if projected > instantly_limit * 1.1:
                alerts.append(("warning", f"Instantly pace: projected {int(projected)} contacts by month end (limit: {instantly_limit})."))

    # --- 3. Apollo Credits (from state file — updated by pipelines) ---
    apollo_limit = limits.get("apollo_credits", 3000)
    apollo_used = state.get("apollo_credits_used", 0)

    report["apollo"] = {
        "credits_used": apollo_used,
        "limit": apollo_limit,
        "remaining": apollo_limit - apollo_used,
    }

    if apollo_used > 0:
        usage_pct = apollo_used / apollo_limit if apollo_limit else 0
        report["apollo"]["usage_percent"] = round(usage_pct * 100, 1)

        if usage_pct >= crit_pct:
            alerts.append(("critical", f"Apollo credits at {usage_pct:.0%} ({apollo_used}/{apollo_limit}). Will run out soon!"))
        elif usage_pct >= warn_pct:
            alerts.append(("warning", f"Apollo credits at {usage_pct:.0%} ({apollo_used}/{apollo_limit})."))

        # Daily credits remaining
        credits_remaining = apollo_limit - apollo_used
        daily_budget = credits_remaining / days_remaining if days_remaining > 0 else 0
        report["apollo"]["safe_daily_budget"] = round(daily_budget, 1)

        if daily_budget < 30:
            alerts.append(("warning", f"Apollo: only {int(daily_budget)} credits/day left for {days_remaining} remaining days."))

    # --- 4. Pipeline daily targets vs actual pace ---
    targets = {
        "AEC": config.get("lead_sourcing", {}).get("daily_target", 13),
        "Startups": config.get("startups", {}).get("daily_target", 12),
        "EU": config.get("eu_outreach", {}).get("daily_target", 8),
    }
    expected_monthly = {k: v * 30 for k, v in targets.items()}
    report["targets"] = {
        "daily": targets,
        "monthly_expected": expected_monthly,
        "total_monthly_expected": sum(expected_monthly.values()),
    }

    # --- 5. Generate report ---
    report["alerts"] = [{"level": a[0], "message": a[1]} for a in alerts]
    report["timestamp"] = now.isoformat()
    report["day_of_month"] = day_of_month
    report["days_remaining"] = days_remaining

    # Save report to logs
    report_file = Path(__file__).parent.parent / "logs" / "monitor_report.json"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    # Update state
    state["last_run"] = now.isoformat()
    _save_state(state)

    # --- 6. Send notifications ---
    if not status_only and alerts:
        critical = [a for a in alerts if a[0] == "critical"]
        warnings = [a for a in alerts if a[0] == "warning"]

        if critical:
            msg = "\n".join(f"🔴 {a[1]}" for a in critical)
            if warnings:
                msg += "\n" + "\n".join(f"🟡 {a[1]}" for a in warnings)
            _send_ntfy(ntfy_topic, "Lead Pipeline CRITICAL", msg, priority="high", tags="rotating_light")
        elif warnings:
            msg = "\n".join(f"🟡 {a[1]}" for a in warnings)
            _send_ntfy(ntfy_topic, "Lead Pipeline Warning", msg, priority="default", tags="warning")

    # Also send a daily summary if no alerts (so you know it's running)
    if not status_only and not alerts and ntfy_topic:
        total_leads = sum(v for v in lead_counts.values() if v >= 0)
        summary = (
            f"All systems normal.\n"
            f"Leads in CRM: {total_leads}\n"
            f"Instantly: {instantly_total}/{instantly_limit} contacts\n"
            f"Apollo: {apollo_used}/{apollo_limit} credits"
        )
        _send_ntfy(ntfy_topic, "Lead Pipeline Daily Report", summary, priority="low", tags="chart_with_upwards_trend")

    return report


def update_apollo_credits(credits_used: int):
    """Called by pipeline scripts to log Apollo credits consumed in a run.

    Usage (at end of any pipeline):
        from src.monitor import update_apollo_credits
        update_apollo_credits(apollo_client._credits_used)
    """
    state = _load_state()
    state = _reset_if_new_month(state)
    state["apollo_credits_used"] = state.get("apollo_credits_used", 0) + credits_used
    _save_state(state)
    logger.info("Apollo credits logged",
                 run_credits=credits_used,
                 month_total=state["apollo_credits_used"])


def update_leads_added(pipeline: str, count: int):
    """Called by pipeline scripts to log leads added in a run.

    Usage:
        from src.monitor import update_leads_added
        update_leads_added("startups", added)
    """
    state = _load_state()
    state = _reset_if_new_month(state)
    leads = state.get("leads_added", {})
    leads[pipeline] = leads.get(pipeline, 0) + count
    state["leads_added"] = leads
    _save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Pipeline Monitor & Alerting")
    parser.add_argument("--status", action="store_true",
                        help="Quick status check (no notifications)")
    args = parser.parse_args()

    config_dir = Path(__file__).parent.parent / "config"
    config = load_config(str(config_dir / "settings.yaml"))

    report = run_monitor(config, status_only=args.status)

    # Print human-readable summary
    print(f"\n{'='*50}")
    print(f"  Pipeline Monitor — {report['timestamp'][:10]}")
    print(f"  Day {report['day_of_month']}/30 ({report['days_remaining']} days remaining)")
    print(f"{'='*50}")

    print(f"\n📊 CRM Lead Counts:")
    for name, count in report.get("lead_counts", {}).items():
        print(f"  {name}: {count}")

    inst = report.get("instantly", {})
    print(f"\n📧 Instantly:")
    print(f"  Total contacts: {inst.get('total_contacts', '?')}/{inst.get('limit', '?')} ({inst.get('usage_percent', '?')}%)")
    for name, count in inst.get("by_campaign", {}).items():
        print(f"    {name}: {count}")

    ap = report.get("apollo", {})
    print(f"\n🔑 Apollo Credits:")
    print(f"  Used: {ap.get('credits_used', '?')}/{ap.get('limit', '?')}")
    print(f"  Remaining: {ap.get('remaining', '?')}")
    if ap.get("safe_daily_budget"):
        print(f"  Safe daily budget: {ap['safe_daily_budget']} credits/day")

    tgt = report.get("targets", {})
    print(f"\n🎯 Daily Targets:")
    for name, t in tgt.get("daily", {}).items():
        print(f"  {name}: {t} leads/day → ~{t*30}/month")
    print(f"  Total: ~{tgt.get('total_monthly_expected', '?')}/month")

    alerts = report.get("alerts", [])
    if alerts:
        print(f"\n⚠️  ALERTS ({len(alerts)}):")
        for a in alerts:
            icon = "🔴" if a["level"] == "critical" else "🟡"
            print(f"  {icon} {a['message']}")
    else:
        print(f"\n✅ All systems normal.")

    print()


if __name__ == "__main__":
    main()
