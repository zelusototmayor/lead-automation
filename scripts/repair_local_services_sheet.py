"""
Repair Local Services Sheet
============================
Fixes data scattered horizontally by gspread's append_row bug.

The bug caused each new lead to be placed ~15 columns to the right of the
previous one instead of on a new row. This script:
1. Reads ALL data from the sheet (including the wide columns)
2. Extracts valid leads from the horizontal scatter
3. Clears the sheet (preserving headers)
4. Writes all leads back as proper rows in columns A-O
"""

import gspread
from google.oauth2.service_account import Credentials
import time
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
CREDENTIALS_FILE = str(Path(__file__).parent.parent / "config" / "google_credentials.json")
SHEET_NAME = "Local Services"
NUM_COLS = 15  # A through O

HEADERS = [
    "ID", "Company", "POC Name", "POC Title", "Phone",
    "Call status", "Notes", "Followup", "Email", "Website",
    "City", "State", "Vertical", "Date Added", "Status",
]


def throttle():
    time.sleep(1.5)


def main():
    print(f"Connecting to Google Sheets...")
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    throttle()
    sheet = spreadsheet.worksheet(SHEET_NAME)
    throttle()

    # Get all data including the wide range
    print("Reading all sheet data (this may take a moment for wide sheets)...")
    all_data = sheet.get_all_values()
    throttle()

    total_rows = len(all_data)
    total_cols = max(len(row) for row in all_data) if all_data else 0
    print(f"Sheet dimensions: {total_rows} rows x {total_cols} columns")

    # Row 1 is headers, rows 2+ are data
    headers_row = all_data[0] if all_data else []
    data_rows = all_data[1:] if len(all_data) > 1 else []

    # Collect all valid leads from the scattered data
    leads = []

    for row_idx, row in enumerate(data_rows):
        # Scan across the row in chunks of NUM_COLS looking for lead data
        # A valid lead chunk starts with an ID matching "LS-" pattern
        col = 0
        while col + NUM_COLS <= len(row):
            chunk = row[col:col + NUM_COLS]
            # Check if this chunk looks like a valid lead (has an LS- ID)
            lead_id = chunk[0].strip()
            company = chunk[1].strip() if len(chunk) > 1 else ""

            if lead_id.startswith("LS-") and company:
                leads.append(chunk)
                col += NUM_COLS
            else:
                col += 1

        # Also check if there's a valid lead starting at col 0 without LS- prefix
        # (in case some early leads were added without proper IDs)
        if row and not row[0].startswith("LS-") and len(row) >= NUM_COLS:
            # Check if first column has any content that looks like a company name
            # but only if we didn't already find a lead at position 0
            first_chunk = row[:NUM_COLS]
            if first_chunk[1].strip() and not any(l == first_chunk for l in leads):
                # Only add if column B (Company) has content and this wasn't captured
                pass  # Skip — we only want LS- prefixed leads to avoid false positives

    # Deduplicate by ID
    seen_ids = set()
    unique_leads = []
    for lead in leads:
        lead_id = lead[0].strip()
        if lead_id not in seen_ids:
            seen_ids.add(lead_id)
            unique_leads.append(lead)

    print(f"\nFound {len(unique_leads)} unique leads in scattered data")

    if not unique_leads:
        print("No leads found! Aborting to avoid data loss.")
        sys.exit(1)

    # Sort by date added (column index 13) for nice ordering
    def sort_key(lead):
        date_str = lead[13] if len(lead) > 13 else ""
        return date_str or "9999"
    unique_leads.sort(key=sort_key)

    # Show a preview
    print("\nFirst 5 leads:")
    for lead in unique_leads[:5]:
        print(f"  {lead[0]} | {lead[1]} | {lead[4]} | {lead[10]}, {lead[11]}")
    print(f"\nLast 5 leads:")
    for lead in unique_leads[-5:]:
        print(f"  {lead[0]} | {lead[1]} | {lead[4]} | {lead[10]}, {lead[11]}")

    # Auto-proceed (data validated above)
    print(f"\nProceeding to repair sheet with {len(unique_leads)} leads...")

    # Clear the entire sheet
    print("Clearing sheet...")
    sheet.clear()
    throttle()

    # Resize to proper dimensions
    print(f"Resizing sheet to {len(unique_leads) + 1} rows x {NUM_COLS} cols...")
    sheet.resize(rows=max(len(unique_leads) + 100, 1000), cols=NUM_COLS)
    throttle()

    # Write headers
    print("Writing headers...")
    sheet.update("A1", [HEADERS])
    throttle()

    # Write leads in batches (to stay under API limits)
    BATCH_SIZE = 50
    for i in range(0, len(unique_leads), BATCH_SIZE):
        batch = unique_leads[i:i + BATCH_SIZE]
        start_row = i + 2  # +1 for header, +1 for 1-indexed
        end_row = start_row + len(batch) - 1
        range_str = f"A{start_row}:O{end_row}"

        # Ensure each row has exactly NUM_COLS columns
        normalized_batch = []
        for lead in batch:
            row = list(lead[:NUM_COLS])
            while len(row) < NUM_COLS:
                row.append("")
            normalized_batch.append(row)

        print(f"Writing rows {start_row}-{end_row}...")
        sheet.update(range_str, normalized_batch)
        throttle()

    print(f"\nRepair complete! {len(unique_leads)} leads written to columns A-O.")
    print("Sheet has been resized to remove trailing empty columns.")


if __name__ == "__main__":
    main()
