"""
Migrate Old Sheets → v2 Tabs + v2 Instantly Campaigns
=======================================================
One-time migration:

  1. Fixes Startups + EU v2 sequence templates in Instantly (replaces the
     broken {{body}} placeholder with real 4-email copy using the
     individual placeholders that personalize.py actually emits —
     {{personalized_opener}}, {{specific_pain_point}},
     {{industry_specific_insight}}, {{signal_hook}}).

  2. For each old sheet → v2 sheet → v2 Instantly campaign triplet:
     - Reads all rows with email from the old sheet.
     - Skips leads marked "Replied" (they engaged positively; don't re-burn them).
     - Looks up matching archived Instantly lead by email → pulls personalization.
     - Writes the lead to the v2 sheet with clean status (status="New",
       all Email N Sent cleared, tagged "Migrated from <old tab>").
     - Pushes to the corresponding v2 Instantly campaign with custom_variables.

  3. Reports counts per campaign.

Safety:
  - v2 Instantly campaigns stay PAUSED throughout.
  - Old tabs are never modified.
  - Leads already present in the v2 sheet (by email) are skipped.

Usage:
    python scripts/migrate_old_sheets_to_v2.py --dry-run
    python scripts/migrate_old_sheets_to_v2.py
"""

import os
import sys
import argparse
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_dotenv():
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# --- Campaign mapping ------------------------------------------------------

CAMPAIGNS = [
    {
        "key": "aec",
        "old_sheet": "AEC Leads",
        "new_sheet": "AEC Leads v2",
        "new_campaign_id": "87e2a34a-9349-4c37-beb8-7e31f67d51ab",
        "archived_campaign_id": "6adb10ba-1439-4046-80f9-900127633995",
    },
    {
        "key": "startups",
        "old_sheet": "B2B Startups",
        "new_sheet": "B2B Startups v2",
        "new_campaign_id": "386d8bf5-c27a-40ac-b208-ac934de1d899",
        "archived_campaign_id": "2899d58a-1d42-4fa8-9a73-699b3a7e51f3",
    },
    {
        "key": "eu",
        "old_sheet": "EU B2B Leads",
        "new_sheet": "EU B2B Leads v2",
        "new_campaign_id": "5021ae54-3e4f-435f-92fe-76598e98a673",
        "archived_campaign_id": "350010d5-2ee6-4b3a-9a84-5c44ce854239",
    },
]


# --- Startups + EU sequence templates ---------------------------------------
# These replace the broken {{body}} placeholders. They use only the custom
# variables that personalize.py + startups.py/eu_outreach.py actually emit.

STARTUPS_SUBJECTS = [
    "{{firstName}}, swap your SDR team for this?",
    "{{companyName}} is hiring reps — have you tried this?",
    "Outbound without the SDRs",
]

STARTUPS_BODIES = [
    # Email 1 (day 0)
    "Hi {{firstName}},\n\n"
    "{{personalized_opener}}\n\n"
    "This email found {{companyName}}, researched you, wrote itself, and\n"
    "landed in your inbox — automatically. That's what I build: a system that\n"
    "finds your ICP, researches them, writes personalized outreach, and books\n"
    "meetings. For a fraction of a single SDR's fully-loaded cost.\n\n"
    "{{specific_pain_point}}\n\n"
    "Worth 15 minutes to see if it fits how {{companyName}} grows pipeline?\n\n"
    "Just reply \"yes\" and I'll send a couple of times.\n\n"
    "Best,\n"
    "Ze Lu Sottomayor",

    # Email 2 (day 3)
    "Hi {{firstName}},\n\n"
    "Quick nudge on the note below.\n\n"
    "{{signal_hook}}\n\n"
    "Short version: an always-on outbound system running in the background —\n"
    "pipeline grows without SDR headcount, and your existing reps focus on\n"
    "closing instead of prospecting.\n\n"
    "Open to a 15-minute look?\n\n"
    "Best,\n"
    "Ze Lu Sottomayor",

    # Email 3 (day 7)
    "Hi {{firstName}},\n\n"
    "One more thought —\n\n"
    "{{industry_specific_insight}}\n\n"
    "Most B2B teams we work with effectively replaced their first SDR hire\n"
    "with this. Same pipeline output, small fraction of the cost. Existing\n"
    "reps keep closing; the system handles top-of-funnel.\n\n"
    "Want me to sketch what it would look like for {{companyName}}?\n\n"
    "Best,\n"
    "Ze Lu Sottomayor",

    # Email 4 (day 12)
    "Hi {{firstName}},\n\n"
    "I'll assume the timing isn't right — no worries.\n\n"
    "If a more predictable pipeline ever becomes a priority at {{companyName}},\n"
    "just reply to this thread and I'll pick it up.\n\n"
    "Cheers,\n"
    "Ze Lu Sottomayor",
]

EU_SUBJECTS = [
    "{{firstName}}, outbound without hiring SDRs",
    "I saw {{companyName}} is hiring reps",
    "Sales pipeline for {{companyName}}",
]

EU_BODIES = [
    # Email 1 (day 0)
    "Hi {{firstName}},\n\n"
    "{{personalized_opener}}\n\n"
    "This email found {{companyName}}, researched you, wrote itself, and\n"
    "landed in your inbox — automatically. That's what I build: a system that\n"
    "finds your ideal prospects across Europe, writes personalized outreach,\n"
    "sends sequences, and books meetings — without scaling SDR headcount.\n\n"
    "{{specific_pain_point}}\n\n"
    "Worth 15 minutes to see if it fits how {{companyName}} grows pipeline?\n\n"
    "Just reply \"yes\" and I'll send a couple of times.\n\n"
    "Best,\n"
    "Ze Lu Sottomayor",

    # Email 2 (day 3)
    "Hi {{firstName}},\n\n"
    "Quick nudge on the note below.\n\n"
    "{{signal_hook}}\n\n"
    "Short version: an always-on outbound engine running in the background —\n"
    "qualified conversations without scaling headcount, and your existing\n"
    "reps focus on closing instead of prospecting.\n\n"
    "Open to a 15-minute look?\n\n"
    "Best,\n"
    "Ze Lu Sottomayor",

    # Email 3 (day 7)
    "Hi {{firstName}},\n\n"
    "One more thought —\n\n"
    "{{industry_specific_insight}}\n\n"
    "Most B2B teams in Europe we work with replaced their first SDR hire with\n"
    "this. Same pipeline output, small fraction of the cost.\n\n"
    "Want me to sketch what it would look like for {{companyName}}?\n\n"
    "Best,\n"
    "Ze Lu Sottomayor",

    # Email 4 (day 12)
    "Hi {{firstName}},\n\n"
    "I'll assume the timing isn't right — no worries.\n\n"
    "If a more predictable pipeline ever becomes a priority at {{companyName}},\n"
    "just reply to this thread and I'll pick it up.\n\n"
    "Cheers,\n"
    "Ze Lu Sottomayor",
]

DELAYS = [0, 3, 7, 12]


def build_sequence(subject_variants: list[str], bodies: list[str]) -> list[dict]:
    steps = []
    for i, (delay, body) in enumerate(zip(DELAYS, bodies)):
        if i == 0:
            subjects = subject_variants
        else:
            subjects = [f"Re: {s}" for s in subject_variants]
        steps.append({
            "step_number": i + 1,
            "type": "email",
            "delay": delay,
            "variants": [{"subject": s, "body": body} for s in subjects],
        })
    return [{"steps": steps}]


# --- Main ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--campaign", choices=["aec", "startups", "eu", "all"],
                        default="all")
    parser.add_argument("--skip-templates", action="store_true",
                        help="Skip the Startups/EU template fix")
    parser.add_argument("--repersonalize", action="store_true",
                        help="Run Claude personalize.py on leads without "
                             "archived personalization")
    args = parser.parse_args()

    _load_dotenv()

    import gspread
    from google.oauth2.service_account import Credentials
    from src.outreach.instantly_client import InstantlyClient
    from src.crm.sheets import GoogleSheetsCRM

    api_key = os.environ["INSTANTLY_API_KEY"]
    client = InstantlyClient(api_key)

    # Personalizer (optional, for leads without archived personalization)
    personalizer = None
    aec_verticals = None
    if args.repersonalize:
        import yaml
        from src.outreach.personalize import EmailPersonalizer
        with open("config/email_templates.yaml") as f:
            tpl = yaml.safe_load(f)
        aec_verticals = tpl.get("aec_verticals", {})
        personalizer = EmailPersonalizer(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model="claude-sonnet-4-20250514",
        )

    SPREADSHEET_ID = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
    CREDS_FILE = "config/google_credentials.json"

    creds = Credentials.from_service_account_file(
        CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    # ------------------------------------------------------------------
    # Step 1: Fix Startups + EU sequence templates in Instantly
    # ------------------------------------------------------------------
    if not args.skip_templates and not args.dry_run:
        print("\n=== Step 1: Fixing Startups + EU sequence templates ===")
        startups_seq = build_sequence(STARTUPS_SUBJECTS, STARTUPS_BODIES)
        eu_seq = build_sequence(EU_SUBJECTS, EU_BODIES)

        for cid, seq, name in [
            ("386d8bf5-c27a-40ac-b208-ac934de1d899", startups_seq, "Startups — Outbound System"),
            ("5021ae54-3e4f-435f-92fe-76598e98a673", eu_seq, "EU B2B — Outbound System"),
        ]:
            print(f"  Pushing new sequence → {name} ...", end=" ", flush=True)
            r = client.set_campaign_sequences(cid, seq)
            print("OK" if r else "ERROR")
            time.sleep(1)

    # ------------------------------------------------------------------
    # Step 2: Migrate leads
    # ------------------------------------------------------------------
    campaigns_to_run = (
        CAMPAIGNS if args.campaign == "all"
        else [c for c in CAMPAIGNS if c["key"] == args.campaign]
    )

    totals = {"read": 0, "skipped_replied": 0, "skipped_dup": 0,
              "written_sheet": 0, "pushed_instantly": 0}

    # Per-campaign sender info for re-personalization
    import yaml
    with open("config/settings.yaml") as f:
        cfg_raw = yaml.safe_load(f)

    def _sub_env(obj):
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            return os.environ.get(obj[2:-1], "")
        elif isinstance(obj, dict):
            return {k: _sub_env(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_sub_env(i) for i in obj]
        return obj
    cfg = _sub_env(cfg_raw)

    SENDER_INFO = {
        "aec": {
            "bio": cfg["personalization"]["sender_bio"],
            "value_proposition": cfg["personalization"]["value_proposition"],
            "aec_verticals": aec_verticals,
        },
        "startups": {
            "bio": cfg["startups"]["personalization"]["sender_bio"],
            "value_proposition": cfg["startups"]["personalization"]["value_proposition"],
            "aec_verticals": {},
        },
        "eu": {
            "bio": cfg["eu_outreach"]["personalization"]["sender_bio"],
            "value_proposition": cfg["eu_outreach"]["personalization"]["value_proposition"],
            "aec_verticals": {},
        },
    }

    for cc in campaigns_to_run:
        print(f"\n=== Migrating [{cc['old_sheet']}] → [{cc['new_sheet']}] + Instantly ===")

        # Read old sheet raw values
        old_ws = sh.worksheet(cc["old_sheet"])
        vals = old_ws.get_all_values()
        header = vals[0]
        rows = vals[1:]
        time.sleep(1)

        # Build index lookup
        idx = {h: i for i, h in enumerate(header)}
        # Note: "Notes" appears twice — idx above maps to the LAST occurrence;
        # we want the first (column G). Fix manually.
        if "Notes" in header:
            idx["Notes"] = header.index("Notes")

        def col(row, key, default=""):
            i = idx.get(key)
            if i is None or i >= len(row):
                return default
            return row[i]

        # Filter: must have email, skip Replied
        candidates = []
        replied_count = 0
        for r in rows:
            email = col(r, "Email").strip()
            if not email:
                continue
            status = col(r, "Status").strip().lower()
            if status in ("replied", "unsubscribed", "bounced", "bad email"):
                replied_count += 1
                continue
            response = col(r, "Response").strip()
            if response and "replied" in response.lower():
                replied_count += 1
                continue
            candidates.append(r)

        print(f"  Old sheet: {len(rows)} rows, {len(candidates)} candidates, "
              f"{replied_count} skipped (replied/bounced)")
        totals["read"] += len(rows)
        totals["skipped_replied"] += replied_count

        # Pull personalization from archived Instantly campaign
        print(f"  Fetching personalization from archived Instantly campaign...")
        archived_leads = client.list_leads(
            campaign_id=cc["archived_campaign_id"], limit=100
        ) or []
        personalization_map = {}
        for al in archived_leads:
            email = (al.get("email") or "").strip().lower()
            if not email:
                continue
            payload = al.get("payload", {}) or {}
            p = {}
            for k in ("personalized_opener", "specific_pain_point",
                      "industry_specific_insight", "signal_hook",
                      "suggested_subject", "industry", "city"):
                v = payload.get(k) or al.get(k)
                if v:
                    p[k] = v
            personalization_map[email] = p
        print(f"  Archived Instantly had {len(archived_leads)} leads, "
              f"{len(personalization_map)} with personalization")

        # Read existing emails in v2 sheet to dedupe
        v2_crm = GoogleSheetsCRM(
            credentials_file=CREDS_FILE,
            spreadsheet_id=SPREADSHEET_ID,
            sheet_name=cc["new_sheet"],
        )
        try:
            existing_v2 = v2_crm.get_all_emails()
        except Exception:
            existing_v2 = set()

        tag = f"Migrated from {cc['old_sheet']} ({datetime.now().strftime('%Y-%m-%d')})"

        # Process each candidate
        instantly_batch = []
        written = 0
        dup_skipped = 0

        for r in candidates:
            email = col(r, "Email").strip().lower()
            if email in existing_v2:
                dup_skipped += 1
                continue

            # Split contact_name
            contact_name = col(r, "Contact Name").strip()
            name_parts = contact_name.split() if contact_name else []
            first_name = name_parts[0] if name_parts else ""
            last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

            # Merge personalization from archived Instantly (if any)
            p = personalization_map.get(email, {})

            # If no archived personalization and --repersonalize, call Claude
            if (not p.get("personalized_opener")
                    and personalizer is not None and not args.dry_run):
                lead_for_claude = {
                    "company": col(r, "Company"),
                    "contact_name": contact_name,
                    "title": col(r, "Title"),
                    "industry": col(r, "Industry"),
                    "city": col(r, "City"),
                    "country": col(r, "Country"),
                    "website": col(r, "Website"),
                    "employee_count": col(r, "Employee Count"),
                }
                try:
                    result = personalizer.personalize_email(
                        lead=lead_for_claude,
                        template="",
                        sender_info=SENDER_INFO[cc["key"]],
                    )
                    if result:
                        p = {
                            "personalized_opener": result.get("personalized_opener", ""),
                            "specific_pain_point": result.get("specific_pain_point", ""),
                            "industry_specific_insight": result.get("industry_specific_insight", ""),
                            "suggested_subject": result.get("suggested_subject", ""),
                            "signal_hook": "",
                            "industry": col(r, "Industry"),
                            "city": col(r, "City"),
                        }
                        # Add signal_hook for Startups/EU based on sheet source
                        if cc["key"] in ("startups", "eu"):
                            p["signal_hook"] = (
                                f"I noticed {col(r, 'Company')} is hiring sales reps — "
                                "what if you could get the same pipeline output without the headcount?"
                            )
                except Exception as e:
                    print(f"    WARN: personalize failed for {email}: {e}")

            lead_dict = {
                "company": col(r, "Company"),
                "company_name": col(r, "Company"),
                "contact_name": contact_name,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": col(r, "Phone"),
                "website": col(r, "Website"),
                "industry": col(r, "Industry") or p.get("industry", ""),
                "employee_count": col(r, "Employee Count"),
                "city": col(r, "City") or p.get("city", ""),
                "country": col(r, "Country"),
                "lead_score": col(r, "Lead Score"),
                "title": col(r, "Title"),
                "linkedin": col(r, "LinkedIn"),
                "source": col(r, "Source"),
                "status": "New",
                "notes": tag,
                # Clear engagement fields
                "email_1_sent": "",
                "email_2_sent": "",
                "email_3_sent": "",
                "email_4_sent": "",
                "opens": "",
                "clicks": "",
                "response": "",
                "instantly_status": "",
                "last_contact": "",
                # Carry over personalization for Instantly
                "personalized_opener": p.get("personalized_opener", ""),
                "specific_pain_point": p.get("specific_pain_point", ""),
                "industry_specific_insight": p.get("industry_specific_insight", ""),
                "signal_hook": p.get("signal_hook", ""),
                "suggested_subject": p.get("suggested_subject", ""),
            }

            # Write to v2 sheet
            if args.dry_run:
                written += 1
            else:
                try:
                    v2_crm.add_lead(lead_dict)
                    existing_v2.add(email)
                    written += 1
                except Exception as e:
                    print(f"    WARN: failed to write {email}: {e}")

            # Queue for Instantly push
            instantly_batch.append(lead_dict)

        print(f"  Written to v2 sheet: {written} (dup skipped: {dup_skipped})")
        totals["written_sheet"] += written
        totals["skipped_dup"] += dup_skipped

        # Push to Instantly (batched)
        if args.dry_run:
            print(f"  [dry-run] would push {len(instantly_batch)} leads to Instantly")
            totals["pushed_instantly"] += len(instantly_batch)
        else:
            print(f"  Pushing {len(instantly_batch)} leads to Instantly campaign "
                  f"{cc['new_campaign_id']}...")
            result = client.add_leads_to_campaign(
                cc["new_campaign_id"], instantly_batch
            )
            pushed = len(result) if result else 0
            print(f"    Pushed: {pushed}")
            totals["pushed_instantly"] += pushed

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("MIGRATION COMPLETE")
    print("=" * 60)
    for k, v in totals.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
