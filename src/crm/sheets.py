"""
Google Sheets CRM Integration
=============================
Manages leads in a Google Sheets CRM.
"""

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from typing import Optional
import structlog

logger = structlog.get_logger()

# Define the scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# CRM Column headers
CRM_HEADERS = [
    "ID",
    "Company",
    "Contact Name",
    "Email",
    "Phone",
    "Website",
    "Industry",
    "Employee Count",
    "City",
    "Country",
    "Lead Score",
    "Status",
    "Date Added",
    "Last Contact",
    "Email 1 Sent",
    "Email 2 Sent",
    "Email 3 Sent",
    "Email 4 Sent",
    "Opens",
    "Clicks",
    "Response",
    "Notes",
    "Source",
    "LinkedIn",
    "Title",
    "Instantly Status"
]


class GoogleSheetsCRM:
    """Google Sheets CRM manager."""

    def __init__(self, credentials_file: str, spreadsheet_id: str, sheet_name: str = "Leads"):
        """
        Initialize the CRM.

        Args:
            credentials_file: Path to service account JSON file
            spreadsheet_id: Google Sheets spreadsheet ID
            sheet_name: Name of the sheet to use
        """
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name

        # Authenticate
        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self.client = gspread.authorize(creds)

        # Open spreadsheet
        self.spreadsheet = self.client.open_by_key(spreadsheet_id)

        # Get or create the leads sheet
        self.sheet = self._get_or_create_sheet(sheet_name)

        # Ensure headers exist
        self._ensure_headers()

        logger.info("CRM initialized", spreadsheet_id=spreadsheet_id, sheet=sheet_name)

    def _get_or_create_sheet(self, sheet_name: str):
        """Get existing sheet or create new one."""
        try:
            return self.spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            logger.info("Creating new sheet", sheet_name=sheet_name)
            return self.spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(CRM_HEADERS))

    def _ensure_headers(self):
        """Ensure the sheet has proper headers."""
        current_headers = self.sheet.row_values(1)
        if not current_headers or current_headers != CRM_HEADERS:
            self.sheet.update("A1", [CRM_HEADERS])
            logger.info("Headers updated")

    def _generate_id(self) -> str:
        """Generate a unique lead ID."""
        return f"LEAD-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def add_lead(self, lead_data: dict) -> Optional[str]:
        """
        Add a new lead to the CRM.

        Args:
            lead_data: Dictionary with lead information

        Returns:
            Lead ID if successful, None otherwise
        """
        # Check for duplicates by email
        email = lead_data.get("email", "")
        if email and self.find_lead_by_email(email):
            logger.info("Duplicate lead skipped", email=email)
            return None

        # Check for duplicates by company name + city
        company = lead_data.get("company", "")
        city = lead_data.get("city", "")
        if company and self.find_lead_by_company(company, city):
            logger.info("Duplicate company skipped", company=company, city=city)
            return None

        lead_id = self._generate_id()

        row = [
            lead_id,
            lead_data.get("company", ""),
            lead_data.get("contact_name", ""),
            lead_data.get("email", ""),
            lead_data.get("phone", ""),
            lead_data.get("website", ""),
            lead_data.get("industry", ""),
            str(lead_data.get("employee_count", "")),
            lead_data.get("city", ""),
            lead_data.get("country", ""),
            str(lead_data.get("lead_score", "")),
            lead_data.get("status", "New"),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            "",  # Last Contact
            "FALSE",  # Email 1 Sent
            "FALSE",  # Email 2 Sent
            "FALSE",  # Email 3 Sent
            "FALSE",  # Email 4 Sent
            "0",  # Opens
            "0",  # Clicks
            "",  # Response
            lead_data.get("notes", ""),
            lead_data.get("source", ""),
            lead_data.get("linkedin", ""),
            lead_data.get("title", ""),
            ""  # Instantly Status
        ]

        self.sheet.append_row(row)
        logger.info("Lead added", lead_id=lead_id, company=lead_data.get("company"))
        return lead_id

    def find_lead_by_email(self, email: str) -> Optional[dict]:
        """Find a lead by email address."""
        try:
            cell = self.sheet.find(email, in_column=4)  # Email is column D (4)
            if cell:
                row_values = self.sheet.row_values(cell.row)
                return self._row_to_dict(row_values)
        except gspread.exceptions.CellNotFound:
            pass
        return None

    def find_lead_by_company(self, company: str, city: str = None) -> Optional[dict]:
        """Find a lead by company name (and optionally city)."""
        try:
            cells = self.sheet.findall(company, in_column=2)  # Company is column B (2)
            for cell in cells:
                row_values = self.sheet.row_values(cell.row)
                lead = self._row_to_dict(row_values)
                if city and lead.get("city", "").lower() != city.lower():
                    continue
                return lead
        except gspread.exceptions.CellNotFound:
            pass
        return None

    def get_leads_for_outreach(self, limit: int = 10) -> list[dict]:
        """
        Get leads that are ready for email outreach.

        Returns leads with status 'New' and no emails sent yet.
        """
        all_rows = self.sheet.get_all_values()[1:]  # Skip header

        leads = []
        for row in all_rows:
            lead = self._row_to_dict(row)
            if (
                lead.get("status") == "New"
                and lead.get("email")
                and lead.get("email_1_sent") == "FALSE"
            ):
                leads.append(lead)
                if len(leads) >= limit:
                    break

        logger.info("Found leads for outreach", count=len(leads))
        return leads

    def get_leads_for_followup(self, step: int = 2) -> list[dict]:
        """
        Get leads that need follow-up emails.

        Args:
            step: Which follow-up step (2, 3, or 4)
        """
        all_rows = self.sheet.get_all_values()[1:]  # Skip header

        leads = []
        for row in all_rows:
            lead = self._row_to_dict(row)

            # Skip if already responded
            if lead.get("response"):
                continue

            # Check which email should be sent next
            email_key = f"email_{step}_sent"
            prev_email_key = f"email_{step-1}_sent"

            if (
                lead.get("email")
                and lead.get(prev_email_key) == "TRUE"
                and lead.get(email_key) == "FALSE"
            ):
                leads.append(lead)

        logger.info("Found leads for followup", step=step, count=len(leads))
        return leads

    def update_lead(self, lead_id: str, updates: dict) -> bool:
        """
        Update a lead's information.

        Args:
            lead_id: The lead ID to update
            updates: Dictionary of field updates
        """
        try:
            cell = self.sheet.find(lead_id, in_column=1)  # ID is column A (1)
            if not cell:
                logger.warning("Lead not found", lead_id=lead_id)
                return False

            row_num = cell.row

            # Map field names to column indices
            field_to_col = {header.lower().replace(" ", "_"): i + 1 for i, header in enumerate(CRM_HEADERS)}

            for field, value in updates.items():
                col = field_to_col.get(field.lower())
                if col:
                    self.sheet.update_cell(row_num, col, str(value))

            logger.info("Lead updated", lead_id=lead_id, updates=list(updates.keys()))
            return True

        except Exception as e:
            logger.error("Failed to update lead", lead_id=lead_id, error=str(e))
            return False

    def mark_email_sent(self, lead_id: str, email_step: int) -> bool:
        """Mark that an email was sent to a lead."""
        return self.update_lead(lead_id, {
            f"email_{email_step}_sent": "TRUE",
            "last_contact": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "status": "Contacted"
        })

    def mark_response_received(self, lead_id: str, response_text: str) -> bool:
        """Mark that a response was received from a lead."""
        return self.update_lead(lead_id, {
            "response": response_text,
            "status": "Replied"
        })

    def get_all_emails(self) -> set:
        """Get all email addresses in the CRM (for deduplication)."""
        try:
            email_col = self.sheet.col_values(4)[1:]  # Column D, skip header
            return set(email.lower() for email in email_col if email)
        except Exception as e:
            logger.error("Failed to get emails", error=str(e))
            return set()

    def get_stats(self) -> dict:
        """Get CRM statistics."""
        all_rows = self.sheet.get_all_values()[1:]

        stats = {
            "total_leads": len(all_rows),
            "new": 0,
            "contacted": 0,
            "replied": 0,
            "won": 0,
            "lost": 0
        }

        for row in all_rows:
            status = row[11].lower() if len(row) > 11 else ""  # Status column
            if status == "new":
                stats["new"] += 1
            elif status == "contacted":
                stats["contacted"] += 1
            elif status == "replied":
                stats["replied"] += 1
            elif status == "won":
                stats["won"] += 1
            elif status == "lost":
                stats["lost"] += 1

        return stats

    def _row_to_dict(self, row: list) -> dict:
        """Convert a row to a dictionary."""
        # Pad row if needed
        while len(row) < len(CRM_HEADERS):
            row.append("")

        return {
            "id": row[0],
            "company": row[1],
            "contact_name": row[2],
            "email": row[3],
            "phone": row[4],
            "website": row[5],
            "industry": row[6],
            "employee_count": row[7],
            "city": row[8],
            "country": row[9],
            "lead_score": row[10],
            "status": row[11],
            "date_added": row[12],
            "last_contact": row[13],
            "email_1_sent": row[14],
            "email_2_sent": row[15],
            "email_3_sent": row[16],
            "email_4_sent": row[17],
            "opens": row[18] if len(row) > 18 else "0",
            "clicks": row[19] if len(row) > 19 else "0",
            "response": row[20] if len(row) > 20 else "",
            "notes": row[21] if len(row) > 21 else "",
            "source": row[22] if len(row) > 22 else "",
            "linkedin": row[23] if len(row) > 23 else "",
            "title": row[24] if len(row) > 24 else "",
            "instantly_status": row[25] if len(row) > 25 else ""
        }

    def update_from_instantly(self, email: str, instantly_data: dict) -> bool:
        """
        Update lead with data synced from Instantly.

        Args:
            email: Lead email address
            instantly_data: Dict with opens, clicks, status, etc.
        """
        lead = self.find_lead_by_email(email)
        if not lead:
            return False

        updates = {}

        if "opens" in instantly_data:
            updates["opens"] = str(instantly_data["opens"])
        if "clicks" in instantly_data:
            updates["clicks"] = str(instantly_data["clicks"])
        if "instantly_status" in instantly_data:
            updates["instantly_status"] = instantly_data["instantly_status"]
        if "response" in instantly_data and instantly_data["response"]:
            updates["response"] = instantly_data["response"]
            updates["status"] = "Replied"

        if updates:
            return self.update_lead(lead["id"], updates)
        return True
