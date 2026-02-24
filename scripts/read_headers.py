import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
CREDS_FILE = "config/google_credentials.json"

scopes = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
client = gspread.authorize(creds)
spreadsheet = client.open_by_key(SPREADSHEET_ID)

for sheet_name in ["Leads", "Local Services"]:
    ws = spreadsheet.worksheet(sheet_name)
    headers = ws.row_values(1)
    print(f"\n{'=' * 60}")
    print(f"Sheet: \"{sheet_name}\"  ({len(headers)} columns)")
    print(f"{'=' * 60}")
    for i, h in enumerate(headers):
        print(f"  [{i:>2}] {h!r}")

print()
