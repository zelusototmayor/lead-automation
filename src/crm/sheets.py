"""
Google Sheets CRM Integration
=============================
Manages leads in a Google Sheets CRM with retry logic and rate limiting.
"""

import gspread
from gspread.exceptions import APIError
from gspread.cell import Cell
from google.oauth2.service_account import Credentials
from datetime import datetime
from typing import Optional
import time
import structlog
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

logger = structlog.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Minimum seconds between API calls (Google quota: 60 req/min per user)
_API_CALL_INTERVAL = 1.5

# CRM Column headers — must match the actual Google Sheet exactly
CRM_HEADERS = [
    "ID",
    "Company",
    "Contact Name",
    "Email",
    "Phone",
    "Status call",
    "Notes",
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
    "Notes",       # Legacy notes column — kept for existing data
    "Title",
    "Instantly Status",
    "Source",
    "LinkedIn",
]

# Column index lookup. "Notes" appears twice (6 and 23); first occurrence wins
# so COL["notes"] = 6 (active column G). Legacy notes at 23 is left alone.
COL = {}
for i, header in enumerate(CRM_HEADERS):
    key = header.lower().replace(" ", "_")
    if key not in COL:
        COL[key] = i


def _is_retryable(exc):
    """Check if a gspread error is retryable (rate limit or transient)."""
    if isinstance(exc, APIError):
        return exc.response.status_code in (429, 500, 503)
    return False


_sheets_retry = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=2, min=5, max=120),
    stop=stop_after_attempt(5),
    before_sleep=lambda rs: logger.warning(
        "Retrying after Sheets API error",
        attempt=rs.attempt_number,
    ),
)


class GoogleSheetsCRM:
    """Google Sheets CRM manager with retry logic and rate limiting."""

    def __init__(self, credentials_file: str, spreadsheet_id: str, sheet_name: str = "Leads"):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._last_api_call = 0
        self._cache = []

        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self.client = gspread.authorize(creds)

        self.spreadsheet = self._api_call(self.client.open_by_key, spreadsheet_id)
        self.sheet = self._get_or_create_sheet(sheet_name)
        self._ensure_headers()
        self._refresh_cache()

        logger.info("CRM initialized", spreadsheet_id=spreadsheet_id, sheet=sheet_name)

    def _throttle(self):
        """Rate-limit API calls to stay under Google Sheets quota."""
        elapsed = time.time() - self._last_api_call
        if elapsed < _API_CALL_INTERVAL:
            time.sleep(_API_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    @_sheets_retry
    def _api_call(self, func, *args, **kwargs):
        """Execute a Sheets API call with throttling and automatic retry on 429/503."""
        self._throttle()
        return func(*args, **kwargs)

    def _refresh_cache(self):
        """Load all sheet data into local cache (single API call)."""
        all_values = self._api_call(self.sheet.get_all_values)
        self._cache = all_values[1:] if len(all_values) > 1 else []
        logger.info("Sheet cache refreshed", rows=len(self._cache))

    def _get_or_create_sheet(self, sheet_name: str):
        try:
            return self._api_call(self.spreadsheet.worksheet, sheet_name)
        except gspread.WorksheetNotFound:
            logger.info("Creating new sheet", sheet_name=sheet_name)
            return self._api_call(
                self.spreadsheet.add_worksheet,
                title=sheet_name, rows=1000, cols=len(CRM_HEADERS)
            )

    def _ensure_headers(self):
        """Ensure headers exist on an empty sheet. Does NOT overwrite existing headers."""
        current_headers = self._api_call(self.sheet.row_values, 1)
        if not current_headers:
            self._api_call(self.sheet.update, "A1", [CRM_HEADERS])
            logger.info("Headers written to empty sheet")

    def _generate_id(self) -> str:
        return f"LEAD-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _find_in_cache(self, col_index: int, value: str) -> Optional[tuple]:
        """Find a row in cache by column value.

        Returns (row_number, row_data) or None.
        row_number is 1-indexed (header=row 1, first data=row 2).
        """
        value_lower = value.lower()
        for i, row in enumerate(self._cache):
            if len(row) > col_index and row[col_index].lower() == value_lower:
                return (i + 2, row)
        return None

    def add_lead(self, lead_data: dict) -> Optional[str]:
        """Add a new lead to the CRM."""
        # Duplicate check by email (local cache, no API call)
        email = lead_data.get("email", "")
        if email and self._find_in_cache(COL["email"], email):
            logger.info("Duplicate lead skipped", email=email)
            return None

        # Duplicate check by company+city (local cache, no API call)
        company = lead_data.get("company", "")
        city = lead_data.get("city", "")
        if company:
            company_lower = company.lower()
            city_lower = city.lower() if city else ""
            for row in self._cache:
                if (len(row) > COL["city"]
                        and row[COL["company"]].lower() == company_lower
                        and (not city or row[COL["city"]].lower() == city_lower)):
                    logger.info("Duplicate company skipped", company=company, city=city)
                    return None

        lead_id = self._generate_id()

        row = [""] * len(CRM_HEADERS)
        row[COL["id"]] = lead_id
        row[COL["company"]] = lead_data.get("company", "")
        row[COL["contact_name"]] = lead_data.get("contact_name", "")
        row[COL["email"]] = lead_data.get("email", "")
        row[COL["phone"]] = lead_data.get("phone", "")
        row[COL["status_call"]] = ""
        row[COL["notes"]] = lead_data.get("notes", "")
        row[COL["website"]] = lead_data.get("website", "")
        row[COL["industry"]] = lead_data.get("industry", "")
        row[COL["employee_count"]] = str(lead_data.get("employee_count", ""))
        row[COL["city"]] = lead_data.get("city", "")
        row[COL["country"]] = lead_data.get("country", "")
        row[COL["lead_score"]] = str(lead_data.get("lead_score", ""))
        row[COL["status"]] = lead_data.get("status", "New")
        row[COL["date_added"]] = datetime.now().strftime("%Y-%m-%d %H:%M")
        row[COL["last_contact"]] = ""
        row[COL["email_1_sent"]] = "FALSE"
        row[COL["email_2_sent"]] = "FALSE"
        row[COL["email_3_sent"]] = "FALSE"
        row[COL["email_4_sent"]] = "FALSE"
        row[COL["opens"]] = "0"
        row[COL["clicks"]] = "0"
        row[COL["response"]] = ""
        # Index 23 is the legacy Notes column — leave empty
        row[COL["title"]] = lead_data.get("title", "")
        row[COL["instantly_status"]] = ""
        row[COL["source"]] = lead_data.get("source", "")
        row[COL["linkedin"]] = lead_data.get("linkedin", "")

        self._api_call(self.sheet.append_row, row, table_range="A1")
        self._cache.append(row)  # Update local cache immediately
        logger.info("Lead added", lead_id=lead_id, company=lead_data.get("company"))
        return lead_id

    def find_lead_by_email(self, email: str) -> Optional[dict]:
        """Find a lead by email address (uses local cache)."""
        match = self._find_in_cache(COL["email"], email)
        if match:
            return self._row_to_dict(list(match[1]))
        return None

    def find_lead_by_company(self, company: str, city: str = None) -> Optional[dict]:
        """Find a lead by company name and optionally city (uses local cache)."""
        company_lower = company.lower()
        for row in self._cache:
            if len(row) > COL["company"] and row[COL["company"]].lower() == company_lower:
                if city and len(row) > COL["city"] and row[COL["city"]].lower() != city.lower():
                    continue
                return self._row_to_dict(list(row))
        return None

    def get_leads_for_outreach(self, limit: int = 10) -> list[dict]:
        """Get leads ready for email outreach (uses local cache)."""
        leads = []
        for row in self._cache:
            lead = self._row_to_dict(list(row))
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
        """Get leads that need follow-up emails (uses local cache)."""
        leads = []
        for row in self._cache:
            lead = self._row_to_dict(list(row))
            if lead.get("response"):
                continue
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
        """Update a lead using batch cell update (single API call for all fields)."""
        try:
            match = self._find_in_cache(COL["id"], lead_id)
            if not match:
                logger.warning("Lead not found", lead_id=lead_id)
                return False

            row_num, _ = match
            cache_idx = row_num - 2

            cells = []
            for field, value in updates.items():
                col_idx = COL.get(field.lower())
                if col_idx is not None:
                    cells.append(Cell(row=row_num, col=col_idx + 1, value=str(value)))
                    # Update local cache
                    while len(self._cache[cache_idx]) <= col_idx:
                        self._cache[cache_idx].append("")
                    self._cache[cache_idx][col_idx] = str(value)

            if cells:
                self._api_call(self.sheet.update_cells, cells)

            logger.info("Lead updated", lead_id=lead_id, updates=list(updates.keys()))
            return True

        except Exception as e:
            logger.error("Failed to update lead", lead_id=lead_id, error=str(e))
            return False

    def batch_update_leads(self, updates: list[tuple[str, dict]]) -> int:
        """Batch-update multiple leads in minimal API calls.

        Args:
            updates: list of (lead_id, {field: value}) tuples

        Returns:
            Number of leads successfully updated.
        """
        all_cells = []
        updated_count = 0
        CHUNK_SIZE = 200

        for lead_id, field_updates in updates:
            match = self._find_in_cache(COL["id"], lead_id)
            if not match:
                logger.warning("Batch: lead not found", lead_id=lead_id)
                continue

            row_num, _ = match
            cache_idx = row_num - 2

            for field, value in field_updates.items():
                col_idx = COL.get(field.lower())
                if col_idx is None:
                    continue
                str_value = str(value)
                all_cells.append(Cell(row=row_num, col=col_idx + 1, value=str_value))
                # Update local cache
                while len(self._cache[cache_idx]) <= col_idx:
                    self._cache[cache_idx].append("")
                self._cache[cache_idx][col_idx] = str_value

            updated_count += 1

        # Write in chunks
        for i in range(0, len(all_cells), CHUNK_SIZE * 10):
            chunk = all_cells[i:i + CHUNK_SIZE * 10]
            if chunk:
                self._api_call(self.sheet.update_cells, chunk)

        logger.info("Batch update complete", leads=updated_count, cells=len(all_cells))
        return updated_count

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
        """Get all email addresses in the CRM (uses local cache)."""
        return set(
            row[COL["email"]].lower() for row in self._cache
            if len(row) > COL["email"] and row[COL["email"]]
        )

    def get_stats(self) -> dict:
        """Get CRM statistics (uses local cache)."""
        stats = {
            "total_leads": len(self._cache),
            "new": 0,
            "contacted": 0,
            "replied": 0,
            "won": 0,
            "lost": 0
        }

        for row in self._cache:
            status = row[COL["status"]].lower() if len(row) > COL["status"] else ""
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
        """Convert a row to a dictionary using COL mapping."""
        while len(row) < len(CRM_HEADERS):
            row.append("")
        return {key: row[idx] for key, idx in COL.items()}

    def update_from_instantly(self, email: str, instantly_data: dict) -> bool:
        """Update lead with data synced from Instantly."""
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

        # Update Email X Sent columns based on emails_sent_count
        emails_sent = instantly_data.get("emails_sent_count", 0)
        if emails_sent > 0:
            for step in range(1, min(emails_sent, 4) + 1):
                updates[f"email_{step}_sent"] = "TRUE"
            updates["last_contact"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Store Instantly status for reference
        instantly_status = instantly_data.get("instantly_status", "")

        # Handle replies (highest priority status)
        if "response" in instantly_data and instantly_data["response"]:
            updates["response"] = instantly_data["response"]
            updates["status"] = "Replied"
        # Status upgrade: only promote New/Queued to Contacted when emails were actually sent
        elif emails_sent > 0 and lead.get("status") in ("New", "Queued"):
            updates["status"] = "Contacted"
        # If Instantly says Active but no emails sent yet, mark as Queued (not Contacted)
        elif emails_sent == 0 and instantly_status == "Active" and lead.get("status") in ("New", "Contacted"):
            updates["status"] = "Queued"
            # Correct falsely set email_sent flags from previous buggy syncs
            if lead.get("email_1_sent") == "TRUE":
                updates["email_1_sent"] = "FALSE"
                logger.warning(
                    "Correcting false contacted",
                    email=email,
                    instantly_status=instantly_status,
                    was_status=lead.get("status"),
                )

        if updates:
            return self.update_lead(lead["id"], updates)
        return True
