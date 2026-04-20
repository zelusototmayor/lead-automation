"""
Setup v2 Instantly Campaigns
==============================
Performs Actions 2a-2d from the Monday 2026-04-20 v2 restart plan:

  2a. List current campaigns.
  2b. Pause + rename the 3 old campaigns with [ARCHIVED] prefix.
  2c. Create 4 new campaigns (AEC, Startups, EU B2B, Logistics) — all PAUSED.
  2d. Configure each campaign: inbox, schedule, sequences, daily limit.

Hard rules (do not change):
  - Never call /campaigns/{id}/activate. All 4 new campaigns stay PAUSED.
  - No https:// URLs in Logistics email bodies.
  - Only max@zelusottomayor.com as try-it address in Logistics.

Usage:
    python scripts/setup_v2_campaigns.py [--list-only]
"""

import os
import sys
import argparse
import time
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


def load_settings():
    import yaml
    _load_dotenv()
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    def sub_env(obj):
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            return os.environ.get(obj[2:-1], "")
        elif isinstance(obj, dict):
            return {k: sub_env(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sub_env(i) for i in obj]
        return obj

    return sub_env(raw)


def load_email_templates():
    import yaml
    tpl_path = Path(__file__).parent.parent / "config" / "email_templates.yaml"
    with open(tpl_path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Sequence builders
# ---------------------------------------------------------------------------

def _build_aec_sequences(cfg, templates):
    """Build the 4-step AEC sequence from email_templates.yaml."""
    emails = templates["sequences"]["default"]["emails"]
    subject_variants = cfg["instantly"]["subject_variants"]

    steps = []
    for i, email in enumerate(emails):
        delay = email["delay_days"]
        if i == 0:
            subjects = subject_variants
        else:
            # Emails 2/3/4: Re: <each variant from email 1>
            subjects = [f"Re: {s}" for s in subject_variants]

        variants = [
            {"subject": subj, "body": email["body_template"]}
            for subj in subjects
        ]
        steps.append({
            "step_number": i + 1,
            "type": "email",
            "delay": delay,
            "variants": variants,
        })

    return [{"steps": steps}]


def _build_placeholder_sequences(subject_variants):
    """
    Build a 4-step sequence where the body is a {{body}} placeholder.
    Used by Startups and EU B2B — personalize.py fills in the real body per lead.
    Day gaps: 0, 3, 7, 12.
    """
    delays = [0, 3, 7, 12]
    body_tpl = "Hi {{firstName}},\n\n{{body}}\n\nBest,\nZe Lu"

    steps = []
    for i, delay in enumerate(delays):
        if i == 0:
            subjects = subject_variants
        else:
            subjects = [f"Re: {s}" for s in subject_variants]

        variants = [
            {"subject": subj, "body": body_tpl}
            for subj in subjects
        ]
        steps.append({
            "step_number": i + 1,
            "type": "email",
            "delay": delay,
            "variants": variants,
        })

    return [{"steps": steps}]


def _build_logistics_sequences(subject_variants):
    """
    Build the 4-step Logistics sequence using the copy from the handoff doc.
    No https:// URLs — only max@zelusottomayor.com as try-it address.
    Day gaps: 0, 3, 7, 12.
    """
    bodies = [
        # Email 1
        (
            "Hi {{firstName}},\n\n"
            "Noticed {{companyName}} works SMB freight — broker / forwarder space is\n"
            "brutal right now with shippers wanting turnarounds in hours, not days.\n\n"
            "I built an agent that reads an inbound quote request, pulls rates from\n"
            "your carriers, and drafts the reply email — so your team turns around\n"
            "quotes in minutes instead of hours.\n\n"
            "Want to see it work on one of your own requests? I can set up a try-it\n"
            "for {{companyName}} — no integration, just forward a quote email to\n"
            "max@zelusottomayor.com and I'll send back what the agent produced.\n\n"
            "Worth 15 min?\n\n"
            "Best,\n"
            "Ze Lu"
        ),
        # Email 2
        (
            "Hi {{firstName}},\n\n"
            "Nudging on the note below.\n\n"
            "Most small freight brokers I talk to lose deals not on price but on\n"
            "response time — shipper sends out 5 RFQs at 9am, books with whoever\n"
            "replies first. The agent closes that gap.\n\n"
            "If the \"forward a quote to max@zelusottomayor.com\" try-it sounds useful,\n"
            "just reply \"send it\" and I'll walk you through.\n\n"
            "Best,\n"
            "Ze Lu"
        ),
        # Email 3
        (
            "Hi {{firstName}},\n\n"
            "One more —\n\n"
            "The agent handles the annoying parts: matching lane + equipment, pulling\n"
            "the right carrier rate, writing the reply in your tone. Your team still\n"
            "reviews before sending.\n\n"
            "Typical fit: 3–15 people, tired of opening spreadsheets at 7am for\n"
            "next-day quotes.\n\n"
            "Worth a quick look?\n\n"
            "Best,\n"
            "Ze Lu"
        ),
        # Email 4
        (
            "Hi {{firstName}},\n\n"
            "I'll assume quote speed isn't a priority right now — no worries.\n\n"
            "If it ever becomes one, reply to this thread and I'll pick it back up.\n\n"
            "Cheers,\n"
            "Ze Lu"
        ),
    ]

    delays = [0, 3, 7, 12]

    # Email 4 has its own subject
    email4_subjects = [f"Closing loop on {{{{companyName}}}}"]

    steps = []
    for i, (delay, body) in enumerate(zip(delays, bodies)):
        if i == 0:
            subjects = subject_variants
        elif i == 3:
            subjects = email4_subjects
        else:
            subjects = [f"Re: {s}" for s in subject_variants]

        variants = [
            {"subject": subj, "body": body}
            for subj in subjects
        ]
        steps.append({
            "step_number": i + 1,
            "type": "email",
            "delay": delay,
            "variants": variants,
        })

    return [{"steps": steps}]


# ---------------------------------------------------------------------------
# Campaign configuration table
# ---------------------------------------------------------------------------

def build_campaign_configs(cfg, templates):
    """
    Returns the 4 new campaign configs, pulling all names/variants from settings.yaml.
    """
    aec_variants = cfg["instantly"]["subject_variants"]
    startups_variants = cfg["startups"]["instantly"]["subject_variants"]
    eu_variants = cfg["eu_outreach"]["instantly"]["subject_variants"]
    logistics_variants = cfg["logistics"]["instantly"]["subject_variants"]

    return [
        {
            "name": cfg["instantly"]["campaign_name"],         # AEC — Outbound System
            "inbox": "ana@zeluautomations.com",
            # America/Detroit = Eastern time (valid Instantly code for EST/EDT)
            "timezone": "America/Detroit",
            "daily_limit": cfg["lead_sourcing"]["daily_target"],  # 15
            "sequences": _build_aec_sequences(cfg, templates),
        },
        {
            "name": cfg["startups"]["instantly"]["campaign_name"],  # Startups — Outbound System
            "inbox": "jose@zeluautomations.com",
            "timezone": "America/Detroit",
            "daily_limit": cfg["startups"]["daily_target"],       # 15
            "sequences": _build_placeholder_sequences(startups_variants),
        },
        {
            "name": cfg["eu_outreach"]["instantly"]["campaign_name"],  # EU B2B — Outbound System
            "inbox": "ana@zelusotto.com",
            # Atlantic/Canary = GMT/BST (UTC+0/UTC+1), closest valid code to Europe/London
            "timezone": "Atlantic/Canary",
            "daily_limit": cfg["eu_outreach"]["daily_target"],        # 12
            "sequences": _build_placeholder_sequences(eu_variants),
        },
        {
            "name": cfg["logistics"]["instantly"]["campaign_name"],   # Logistics — Quote Agent
            "inbox": "jose@zelusotto.com",
            "timezone": "Atlantic/Canary",
            "daily_limit": cfg["logistics"]["daily_target"],          # 15
            "sequences": _build_logistics_sequences(logistics_variants),
        },
    ]


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

OLD_CAMPAIGN_KEYWORDS = [
    "B2B Startups",
    "AEC Business",
    "EU B2B",
    # Also catch variations
    "AEC Outbound",
    "Startups Outbound",
]

NEW_CAMPAIGN_NAMES_SET = {
    "AEC — Outbound System",
    "Startups — Outbound System",
    "EU B2B — Outbound System",
    "Logistics — Quote Agent",
}


def identify_old_campaigns(campaigns: list[dict]) -> list[dict]:
    """
    Return campaigns that look like the 3 old ones (not [ARCHIVED] already,
    not in the 4 new-name set).
    """
    old = []
    for c in campaigns:
        name = c.get("name", "")
        if name.startswith("[ARCHIVED]"):
            continue
        if name in NEW_CAMPAIGN_NAMES_SET:
            continue
        # Must match at least one keyword to be considered old
        if any(kw.lower() in name.lower() for kw in OLD_CAMPAIGN_KEYWORDS):
            old.append(c)
    return old


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Setup v2 Instantly campaigns")
    parser.add_argument("--list-only", action="store_true",
                        help="Just list current campaigns and exit")
    args = parser.parse_args()

    _load_dotenv()
    api_key = os.environ.get("INSTANTLY_API_KEY", "")
    if not api_key:
        print("ERROR: INSTANTLY_API_KEY not found in environment.")
        sys.exit(1)

    from src.outreach.instantly_client import InstantlyClient
    client = InstantlyClient(api_key)

    # ------------------------------------------------------------------
    # 2a. List current campaigns
    # ------------------------------------------------------------------
    print("\n" + "="*60)
    print("Current campaigns in Instantly:")
    print("="*60)
    campaigns = client.list_campaigns()
    if not campaigns:
        print("  (none found or API error)")
    for c in campaigns:
        print(f"  [{str(c.get('status', '?')):10s}] {c.get('id')}  {repr(c.get('name'))}")

    if args.list_only:
        return

    # ------------------------------------------------------------------
    # 2b. Archive 3 old campaigns
    # ------------------------------------------------------------------
    old_campaigns = identify_old_campaigns(campaigns)
    print(f"\n{'='*60}")
    print(f"Archiving {len(old_campaigns)} old campaign(s):")
    print("="*60)

    if not old_campaigns:
        print("  No old campaigns matched — skipping archive step.")
        print("  (If you expected archiving, check campaign names above.)")
    else:
        for c in old_campaigns:
            cid = c["id"]
            old_name = c["name"]
            new_name = f"[ARCHIVED] {old_name}"

            print(f"\n  Archiving: {repr(old_name)}")

            # Pause first (required before rename on some plans)
            print(f"    Pausing ...", end=" ", flush=True)
            ok = client.pause_campaign(cid)
            print("OK" if ok else "WARN (already paused or error)")
            time.sleep(1)

            # Rename with [ARCHIVED] prefix
            print(f"    Renaming to {repr(new_name)} ...", end=" ", flush=True)
            result = client._make_request("PATCH", f"campaigns/{cid}",
                                          data={"name": new_name})
            print("OK" if result is not None else "ERROR")
            time.sleep(1)

    # ------------------------------------------------------------------
    # 2c + 2d. Create 4 new campaigns
    # ------------------------------------------------------------------
    cfg = load_settings()
    templates = load_email_templates()
    campaign_configs = build_campaign_configs(cfg, templates)

    print(f"\n{'='*60}")
    print("Creating 4 new v2 campaigns (all PAUSED):")
    print("="*60)

    created = []
    for cc in campaign_configs:
        name = cc["name"]
        print(f"\n  [{name}]")

        # Skip if already exists with this exact name
        existing = next((c for c in client.list_campaigns()
                         if c.get("name") == name), None)
        if existing:
            cid = existing["id"]
            print(f"    Already exists: {cid} — using existing.")
        else:
            print(f"    Creating ...", end=" ", flush=True)
            # Instantly V2 requires campaign_schedule in the creation body
            schedule_payload = {
                "schedules": [{
                    "name": "Default",
                    "timing": {"from": "09:00", "to": "17:00"},
                    "days": {
                        "0": False,  # Sun
                        "1": True,   # Mon
                        "2": True,   # Tue
                        "3": True,   # Wed
                        "4": True,   # Thu
                        "5": True,   # Fri
                        "6": False,  # Sat
                    },
                    "timezone": cc["timezone"],
                }]
            }
            result = client._make_request("POST", "campaigns", data={
                "name": name,
                "campaign_schedule": schedule_payload,
            })
            if not result:
                print("ERROR — skipping this campaign.")
                continue
            cid = result["id"]
            print(f"OK → {cid}")
            time.sleep(1)

        # Assign sending account (exactly one inbox)
        print(f"    Assigning inbox {cc['inbox']} ...", end=" ", flush=True)
        r = client._make_request("PATCH", f"campaigns/{cid}",
                                 data={"email_list": [cc["inbox"]]})
        print("OK" if r is not None else "ERROR")
        time.sleep(1)

        # Set email sequences
        print(f"    Setting sequences ...", end=" ", flush=True)
        r = client.set_campaign_sequences(cid, cc["sequences"])
        print("OK" if r is not None else "ERROR")
        time.sleep(1)

        # Set daily sending limit
        print(f"    Setting daily limit {cc['daily_limit']} ...", end=" ", flush=True)
        r = client._make_request("PATCH", f"campaigns/{cid}",
                                 data={"daily_limit": cc["daily_limit"]})
        print("OK" if r is not None else "ERROR")
        time.sleep(1)

        # Verify / enforce PAUSED — NEVER activate
        print(f"    Ensuring campaign is PAUSED ...", end=" ", flush=True)
        campaign_detail = client.get_campaign(cid)
        status = (campaign_detail or {}).get("status", "unknown")
        if status not in ("paused", "draft"):
            client.pause_campaign(cid)
            print(f"paused (was: {status})")
        else:
            print(f"already {status}")

        created.append({"id": cid, "name": name, "inbox": cc["inbox"]})
        time.sleep(1)

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("Summary — 4 new v2 campaigns:")
    print("="*60)
    for c in created:
        print(f"  {c['id']}  {c['name']}  ({c['inbox']})")

    # Confirm none are active
    print("\nVerifying all new campaigns are PAUSED...")
    all_camps = client.list_campaigns()
    new_names = {cc["name"] for cc in campaign_configs}
    for c in all_camps:
        if c.get("name") in new_names:
            status = c.get("status", "?")
            flag = "OK" if status in ("paused", "draft") else "WARNING: NOT PAUSED"
            print(f"  {c.get('name'):40s} status={status}  {flag}")

    print("\nDone. Ze flips campaigns live himself Monday morning.")


if __name__ == "__main__":
    main()
