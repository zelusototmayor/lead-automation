#!/usr/bin/env python3
"""
Diagnostic script: Inspect the "Local Services" sheet to find where all leads are.
"""

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
SHEET_NAME = "Local Services"
CREDENTIALS_PATH = "config/google_credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def main():
    # Authenticate
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    gc = gspread.authorize(creds)

    # Open spreadsheet and worksheet
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.worksheet(SHEET_NAME)

    print(f"=== Inspecting sheet: '{SHEET_NAME}' ===")
    print(f"Spreadsheet ID: {SPREADSHEET_ID}")
    print(f"Worksheet row_count (capacity): {worksheet.row_count}")
    print(f"Worksheet col_count (capacity): {worksheet.col_count}")
    print()

    # --- 1. get_all_values() ---
    all_values = worksheet.get_all_values()
    total_rows = len(all_values)
    print(f"[1] get_all_values() returned {total_rows} rows")
    print()

    # --- 2 & 3. Print every non-empty row ---
    non_empty_rows = []
    empty_rows = []

    for i, row in enumerate(all_values):
        row_num = i + 1  # 1-indexed
        has_data = any(cell.strip() for cell in row)
        if has_data:
            non_empty_rows.append((row_num, row))
        else:
            empty_rows.append(row_num)

    print(f"[2] Non-empty rows: {len(non_empty_rows)}")
    print(f"    Empty rows:     {len(empty_rows)}")
    print()

    print("[3] ALL non-empty rows (row# | first 3 cells):")
    print("-" * 80)
    for row_num, row in non_empty_rows:
        first3 = row[:3]
        # Truncate long cell values for readability
        display = [c[:50] if len(c) > 50 else c for c in first3]
        print(f"  Row {row_num:>4}: {display}")
    print("-" * 80)
    print()

    # --- 4. Check rows 100-200 specifically ---
    print("[4] Checking rows 100-200 specifically:")
    if total_rows < 100:
        print(f"    get_all_values() only returned {total_rows} rows, so rows 100-200 are NOT in the result.")
        print("    Trying a direct range fetch for rows 100-200...")

        # Direct API call for A100:Z200
        try:
            range_data = worksheet.get("A100:Z200")
            if range_data:
                print(f"    Direct fetch A100:Z200 returned {len(range_data)} rows with data:")
                for j, row in enumerate(range_data):
                    actual_row = 100 + j
                    first3 = row[:3] if len(row) >= 3 else row
                    display = [c[:50] if len(c) > 50 else c for c in first3]
                    print(f"      Row {actual_row:>4}: {display}")
            else:
                print("    Direct fetch A100:Z200 returned EMPTY -- no data in that range.")
        except Exception as e:
            print(f"    Direct fetch failed: {e}")
    else:
        found_in_range = 0
        for row_num, row in non_empty_rows:
            if 100 <= row_num <= 200:
                found_in_range += 1
                first3 = row[:3]
                display = [c[:50] if len(c) > 50 else c for c in first3]
                print(f"  Row {row_num:>4}: {display}")
        if found_in_range == 0:
            print("    No non-empty rows found in range 100-200.")
        else:
            print(f"    Found {found_in_range} non-empty rows in range 100-200.")
    print()

    # --- 5. Summary ---
    print("[5] SUMMARY:")
    print(f"    Total rows from get_all_values(): {total_rows}")
    print(f"    Rows with data:                   {len(non_empty_rows)}")
    print(f"    Completely empty rows:             {len(empty_rows)}")
    if non_empty_rows:
        print(f"    First data row:                   {non_empty_rows[0][0]}")
        print(f"    Last data row:                    {non_empty_rows[-1][0]}")
    print()

    # --- 6. List ALL worksheets to check if leads went to another tab ---
    print("[6] All worksheets in this spreadsheet:")
    for ws in spreadsheet.worksheets():
        print(f"    - '{ws.title}' (rows={ws.row_count}, cols={ws.col_count})")


if __name__ == "__main__":
    main()
