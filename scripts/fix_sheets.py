"""
Fix Google Sheets: rename header, insert columns, fix misaligned row.
"""

import gspread
from google.oauth2.service_account import Credentials
import time

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CREDS_PATH = "config/google_credentials.json"
SPREADSHEET_ID = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"

creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

# ── 0. Read current "Leads" headers ──────────────────────────────────────────
leads = spreadsheet.worksheet("Leads")
headers_before = leads.row_values(1)
print("=== BEFORE ===")
for i, h in enumerate(headers_before):
    print(f"  {i}: {h}")
print(f"  Total columns: {len(headers_before)}\n")

time.sleep(2)

# ── 1. Rename G1 from "Notes call" to "Notes" ───────────────────────────────
print("Step 1: Renaming G1 from 'Notes call' to 'Notes'...")
leads.update_acell("G1", "Notes")
print("  Done.\n")

time.sleep(2)

# ── 2. Insert 2 columns at index 24 (before current col 24 = Source) ────────
print("Step 2: Inserting 2 columns at index 24 (before 'Source')...")
sheet_id = leads.id  # numeric sheet/tab id for batch_update

body = {
    "requests": [
        {
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 24,   # 0-based, inserts BEFORE index 24
                    "endIndex": 26,     # exclusive → inserts 2 columns
                },
                "inheritFromBefore": False,
            }
        }
    ]
}
spreadsheet.batch_update(body)
print("  Inserted 2 columns.\n")

time.sleep(2)

# Write headers for the new columns
print("  Writing 'Title' at new col 24 (Y1) and 'Instantly Status' at col 25 (Z1)...")
leads.update_acell("Y1", "Title")
time.sleep(2)
leads.update_acell("Z1", "Instantly Status")
print("  Done.\n")

time.sleep(2)

# ── 3. Verify final Leads headers ────────────────────────────────────────────
print("Step 3: Verifying final Leads headers...")
headers_after = leads.row_values(1)
print("=== AFTER ===")
for i, h in enumerate(headers_after):
    print(f"  {i}: {h}")
print(f"  Total columns: {len(headers_after)}\n")

expected = [
    "ID", "Company", "Contact Name", "Email", "Phone", "Status call",
    "Notes", "Website", "Industry", "Employee Count", "City", "Country",
    "Lead Score", "Status", "Date Added", "Last Contact",
    "Email 1 Sent", "Email 2 Sent", "Email 3 Sent", "Email 4 Sent",
    "Opens", "Clicks", "Response", "Notes", "Title", "Instantly Status",
    "Source", "LinkedIn",
]
if headers_after == expected:
    print("  PASS: Headers match expected layout.\n")
else:
    print("  MISMATCH vs expected:")
    for i, (got, exp) in enumerate(zip(headers_after, expected)):
        flag = " <-- DIFF" if got != exp else ""
        print(f"    {i}: got='{got}' expected='{exp}'{flag}")
    if len(headers_after) != len(expected):
        print(f"    Length: got {len(headers_after)}, expected {len(expected)}")
    print()

time.sleep(2)

# ── 4. Fix misaligned row 32 in "Local Services" sheet ──────────────────────
print("Step 4: Fixing misaligned row 32 in 'Local Services' sheet...")
local_svc = spreadsheet.worksheet("Local Services")

time.sleep(2)

# Read the full row (including empty leading cells)
row32_raw = local_svc.row_values(32)
print(f"  Raw row 32 ({len(row32_raw)} values): {row32_raw}")

# Also read via get to see actual cell range including blanks
time.sleep(2)
# Use get with a range that covers plenty of columns
all_cells_row32 = local_svc.get(f"A32:AZ32")
print(f"  get('A32:AZ32'): {all_cells_row32}")

time.sleep(2)

# row_values strips trailing empties but keeps leading empties as ''
# If ID is in col I (index 8), there should be 8 leading empty strings
# Let's strip leading empty strings to shift data left
stripped = [v for v in row32_raw if v != ""] if row32_raw else []
if not stripped:
    # Fallback: flatten the get result
    flat = all_cells_row32[0] if all_cells_row32 else []
    stripped = [v for v in flat if v != ""]

print(f"  Stripped values ({len(stripped)}): {stripped}")

# Read headers to know how many columns
time.sleep(2)
ls_headers = local_svc.row_values(1)
num_cols = len(ls_headers)
print(f"  Local Services has {num_cols} header columns: {ls_headers}")

# Pad stripped to num_cols
padded = stripped + [""] * (num_cols - len(stripped))

# Clear the entire row first, then write shifted data starting at A32
time.sleep(2)
# Clear from A32 to the last column letter
from gspread.utils import rowcol_to_a1
last_col_letter = rowcol_to_a1(1, num_cols).rstrip("1")  # e.g. "Z"
clear_range = f"A32:{last_col_letter}32"
# Also clear beyond in case data was further right
extended_clear = f"A32:AZ32"
local_svc.batch_clear([extended_clear])
print(f"  Cleared {extended_clear}")

time.sleep(2)
write_range = f"A32:{last_col_letter}32"
local_svc.update(write_range, [padded[:num_cols]])
print(f"  Wrote {len(padded[:num_cols])} values to {write_range}")

time.sleep(2)

# Verify
row32_after = local_svc.row_values(32)
print(f"\n  Row 32 after fix ({len(row32_after)} values): {row32_after}")
print("  Done.\n")

print("All fixes complete.")
