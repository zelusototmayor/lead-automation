"""
Migrate Local Services sheet data to standardized CRM format.
=============================================================
Dry-run by default. Pass --apply to write changes to the sheet.

Usage:
  python scripts/migrate_sheet_data.py          # dry run
  python scripts/migrate_sheet_data.py --apply  # write to sheet
"""

import sys, os, re
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.local_services_sheet import LocalServicesCRM, COL_LS, LOCAL_SERVICES_HEADERS

SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID",
    "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8",
)
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "config/google_credentials.json")
SHEET_NAME = os.getenv("LS_SHEET_NAME", "Local Services")

# ── Mapping rules ──────────────────────────────────────────────────────

CALL_STATUS_MAP = {
    "yes":            "Reached",
    "didn't pick up": "No Answer",
    "didnt pick up":  "No Answer",
    "voicemail":      "Voicemail",
    # already correct values pass through unchanged
}

# Keywords in notes that suggest specific outcomes
NOT_INTERESTED_KEYWORDS = [
    "mandou me passear",
    "not interested",
    "nao quer",
    "não quer",
    "said no",
    "told me no",
    "hung up",
]


def normalize_followup(raw: str) -> tuple[str, str | None]:
    """Convert followup like '23/02 - 8h' to '2026-02-23'.
    Returns (normalized_date, extra_info_for_notes) or ('', None) if invalid.
    Also detects emails accidentally placed in followup column."""

    raw = raw.strip()
    if not raw:
        return ("", None)

    # Detect email in followup field
    if "@" in raw:
        return ("", f"Email found in followup field: {raw}")

    # Already YYYY-MM-DD format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return (raw, None)

    # Extract DD/MM with optional time info
    m = re.match(r"(\d{1,2})/(\d{1,2})(.*)$", raw)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        extra = m.group(3).strip().lstrip("-").strip()
        year = 2026  # current year
        try:
            dt = datetime(year, month, day)
            date_str = dt.strftime("%Y-%m-%d")
            note_extra = f"(original followup time: {extra})" if extra else None
            return (date_str, note_extra)
        except ValueError:
            return ("", f"Invalid followup date: {raw}")

    return ("", f"Unrecognized followup format: {raw}")


def classify_lead(row: list) -> dict:
    """Analyze a row and return proposed changes as {field: (old, new)}."""
    changes = {}

    def val(col_name):
        idx = COL_LS[col_name]
        return row[idx].strip() if len(row) > idx else ""

    lead_id = val("id")
    call_status = val("call_status")
    status = val("status")
    followup = val("followup")
    notes = val("notes")
    email = val("email")

    # 1. Map Call Status
    cs_lower = call_status.lower()
    if cs_lower in CALL_STATUS_MAP:
        new_cs = CALL_STATUS_MAP[cs_lower]
        if new_cs != call_status:
            changes["call_status"] = (call_status, new_cs)

    # 2. Fix empty Status
    if not status:
        changes["status"] = ("", "New")
        status = "New"  # use for downstream logic

    # 3. Promote Status based on call history
    effective_cs = CALL_STATUS_MAP.get(cs_lower, call_status)
    if status == "New" and effective_cs:
        # Check notes for negative signals
        notes_lower = notes.lower()
        is_negative = any(kw in notes_lower for kw in NOT_INTERESTED_KEYWORDS)

        if is_negative:
            changes["status"] = (status, "Not Interested")
        elif effective_cs in ("Reached", "Voicemail", "No Answer", "Gatekeeper"):
            changes["status"] = (status, "Contacted")

    # 4. Normalize followup date
    if followup:
        new_date, extra_note = normalize_followup(followup)

        if new_date != followup:
            changes["followup"] = (followup, new_date)

        # If an email was in the followup field, move it
        if extra_note and "Email found" in extra_note:
            email_match = re.search(r"[\w.-]+@[\w.-]+\.\w+", followup)
            if email_match and not email:
                changes["email"] = (email, email_match.group())
                changes["followup"] = (followup, "")

        # If there was time info, append to notes
        if extra_note and "Email found" not in extra_note:
            changes["_followup_note"] = extra_note

    return changes


def main():
    apply = "--apply" in sys.argv

    print(f"{'=' * 60}")
    print(f"  Local Services Sheet Migration {'(DRY RUN)' if not apply else '*** APPLYING ***'}")
    print(f"{'=' * 60}\n")

    crm = LocalServicesCRM(
        credentials_file=CREDENTIALS_FILE,
        spreadsheet_id=SPREADSHEET_ID,
        sheet_name=SHEET_NAME,
    )

    total_leads = len(crm._cache)
    leads_changed = 0
    change_summary = {
        "call_status": 0,
        "status": 0,
        "followup": 0,
        "email": 0,
    }

    for i, row in enumerate(crm._cache):
        lead_id = row[COL_LS["id"]] if len(row) > COL_LS["id"] else f"ROW-{i+2}"
        company = row[COL_LS["company"]] if len(row) > COL_LS["company"] else "?"

        changes = classify_lead(row)

        if not changes:
            continue

        leads_changed += 1

        # Print changes
        print(f"  [{lead_id}] {company}")
        for field, value in changes.items():
            if field.startswith("_"):
                print(f"    note: {value}")
                continue
            old, new = value
            print(f"    {field}: \"{old}\" -> \"{new}\"")
            if field in change_summary:
                change_summary[field] += 1
        print()

        # Apply if not dry run
        if apply:
            updates = {k: v[1] for k, v in changes.items() if not k.startswith("_")}
            if updates:
                ok = crm.update_lead(lead_id, updates)
                if not ok:
                    print(f"    *** FAILED to update {lead_id} ***")

    # Summary
    print(f"{'=' * 60}")
    print(f"  Summary")
    print(f"{'=' * 60}")
    print(f"  Total leads:    {total_leads}")
    print(f"  Leads changed:  {leads_changed}")
    print(f"  ---")
    for field, count in change_summary.items():
        if count:
            print(f"  {field}: {count} changes")
    print()

    if not apply:
        print("  This was a DRY RUN. No changes were made.")
        print("  Run with --apply to write changes to the sheet.")
    else:
        print("  Changes applied to Google Sheet.")


if __name__ == "__main__":
    main()
