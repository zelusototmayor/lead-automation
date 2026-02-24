"""
Local Services Google Sheets CRM
=================================
Lightweight CRM for local-services phone outreach pipeline.
Separate tab, simpler schema — no email tracking columns.
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
    "https://www.googleapis.com/auth/drive",
]

_API_CALL_INTERVAL = 1.5

LOCAL_SERVICES_HEADERS = [
    "ID",
    "Company",
    "POC Name",
    "POC Title",
    "Phone",
    "Call status",
    "Notes",
    "Followup",
    "Email",
    "Website",
    "City",
    "State",
    "Vertical",
    "Date Added",
    "Status",
]

COL_LS = {header.lower().replace(" ", "_"): i for i, header in enumerate(LOCAL_SERVICES_HEADERS)}


def _is_retryable(exc):
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


class LocalServicesCRM:
    """Google Sheets CRM for local-services phone outreach."""

    def __init__(self, credentials_file: str, spreadsheet_id: str, sheet_name: str = "Local Services"):
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

        logger.info("LocalServicesCRM initialized", spreadsheet_id=spreadsheet_id, sheet=sheet_name)

    def _throttle(self):
        elapsed = time.time() - self._last_api_call
        if elapsed < _API_CALL_INTERVAL:
            time.sleep(_API_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    @_sheets_retry
    def _api_call(self, func, *args, **kwargs):
        self._throttle()
        return func(*args, **kwargs)

    def _refresh_cache(self):
        all_values = self._api_call(self.sheet.get_all_values)
        self._cache = all_values[1:] if len(all_values) > 1 else []
        logger.info("Local services cache refreshed", rows=len(self._cache))

    def _get_or_create_sheet(self, sheet_name: str):
        try:
            return self._api_call(self.spreadsheet.worksheet, sheet_name)
        except gspread.WorksheetNotFound:
            logger.info("Creating new sheet", sheet_name=sheet_name)
            return self._api_call(
                self.spreadsheet.add_worksheet,
                title=sheet_name, rows=1000, cols=len(LOCAL_SERVICES_HEADERS),
            )

    def _ensure_headers(self):
        """Ensure headers exist on an empty sheet. Does NOT overwrite existing headers."""
        current_headers = self._api_call(self.sheet.row_values, 1)
        if not current_headers:
            self._api_call(self.sheet.update, "A1", [LOCAL_SERVICES_HEADERS])
            logger.info("Local services headers written to empty sheet")

    def _generate_id(self) -> str:
        return f"LS-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def get_all_companies(self) -> set:
        """Return set of (company, city) tuples for fast dedup."""
        results = set()
        for row in self._cache:
            if len(row) > COL_LS["city"]:
                company = row[COL_LS["company"]].strip().lower()
                city = row[COL_LS["city"]].strip().lower()
                if company:
                    results.add((company, city))
        return results

    def add_lead(self, lead_data: dict) -> Optional[str]:
        """Add a lead. Deduplicates by company+city."""
        company = lead_data.get("company", "").strip()
        city = lead_data.get("city", "").strip()

        if not company:
            return None

        # Duplicate check (local cache)
        company_lower = company.lower()
        city_lower = city.lower()
        for row in self._cache:
            if (len(row) > COL_LS["city"]
                    and row[COL_LS["company"]].strip().lower() == company_lower
                    and row[COL_LS["city"]].strip().lower() == city_lower):
                logger.debug("Duplicate local-service lead skipped", company=company, city=city)
                return None

        lead_id = self._generate_id()

        row = [""] * len(LOCAL_SERVICES_HEADERS)
        row[COL_LS["id"]] = lead_id
        row[COL_LS["company"]] = company
        row[COL_LS["poc_name"]] = lead_data.get("poc_name", "")
        row[COL_LS["poc_title"]] = lead_data.get("poc_title", "")
        row[COL_LS["phone"]] = lead_data.get("phone", "")
        row[COL_LS["call_status"]] = ""
        row[COL_LS["notes"]] = ""
        row[COL_LS["followup"]] = ""
        row[COL_LS["email"]] = lead_data.get("email", "")
        row[COL_LS["website"]] = lead_data.get("website", "")
        row[COL_LS["city"]] = city
        row[COL_LS["state"]] = lead_data.get("state", "")
        row[COL_LS["vertical"]] = lead_data.get("vertical", "")
        row[COL_LS["date_added"]] = datetime.now().strftime("%Y-%m-%d %H:%M")
        row[COL_LS["status"]] = lead_data.get("status", "New")

        self._api_call(self.sheet.append_row, row, table_range="A1")
        self._cache.append(row)
        logger.info("Local service lead added", lead_id=lead_id, company=company, city=city)
        return lead_id

    def get_stats(self) -> dict:
        stats = {
            "total_leads": len(self._cache),
            "by_vertical": {},
            "by_metro": {},
        }
        for row in self._cache:
            if len(row) > COL_LS["vertical"]:
                vertical = row[COL_LS["vertical"]] or "unknown"
                stats["by_vertical"][vertical] = stats["by_vertical"].get(vertical, 0) + 1
            if len(row) > COL_LS["city"]:
                city = row[COL_LS["city"]] or "unknown"
                stats["by_metro"][city] = stats["by_metro"].get(city, 0) + 1
        return stats

    # ── CRM Methods for Cold Calling Dashboard ──────────────────────────

    def _row_to_dict(self, row: list) -> dict:
        """Convert a row list to a dict using COL_LS mapping."""
        while len(row) < len(LOCAL_SERVICES_HEADERS):
            row.append("")
        return {key: row[idx] for key, idx in COL_LS.items()}

    def _find_by_id(self, lead_id: str) -> Optional[tuple]:
        """Find a row by lead ID. Returns (row_number, row_data) or None.
        row_number is 1-indexed (header=1, first data=2)."""
        lead_id_lower = lead_id.lower()
        for i, row in enumerate(self._cache):
            if len(row) > COL_LS["id"] and row[COL_LS["id"]].lower() == lead_id_lower:
                return (i + 2, row)
        return None

    def update_lead(self, lead_id: str, updates: dict) -> bool:
        """Batch update cells for a lead using gspread.cell.Cell."""
        try:
            match = self._find_by_id(lead_id)
            if not match:
                logger.warning("Lead not found", lead_id=lead_id)
                return False

            row_num, _ = match
            cache_idx = row_num - 2

            cells = []
            for field, value in updates.items():
                col_idx = COL_LS.get(field.lower())
                if col_idx is not None:
                    cells.append(Cell(row=row_num, col=col_idx + 1, value=str(value)))
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

    def get_all_leads(self) -> list[dict]:
        """Return all rows as dicts."""
        return [self._row_to_dict(list(row)) for row in self._cache]

    def get_call_queue(self, today_str: str) -> list[dict]:
        """Leads to call: never called + overdue follow-ups.
        Excludes terminal statuses."""
        terminal = {"won", "not interested", "not a fit", "bad data"}
        queue = []
        for row in self._cache:
            d = self._row_to_dict(list(row))
            status = (d.get("status") or "").lower()
            if status in terminal:
                continue
            call_status = (d.get("call_status") or "").strip()
            followup = (d.get("followup") or "").strip()

            # Never called
            if not call_status:
                queue.append(d)
            # Has a follow-up date that's today or past
            elif followup and followup <= today_str:
                queue.append(d)
        return queue

    def log_call(self, lead_id: str, call_status: str, notes: str = "",
                 followup_date: str = "", new_status: str = "") -> bool:
        """Log a call: set call_status, append timestamped notes, set followup and status."""
        match = self._find_by_id(lead_id)
        if not match:
            return False

        row_num, row = match
        cache_idx = row_num - 2

        # Build updates
        updates = {"call_status": call_status}

        # Append timestamped note (newest first)
        if notes:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            new_entry = f"[{timestamp}] {notes}"
            existing = row[COL_LS["notes"]] if len(row) > COL_LS["notes"] else ""
            if existing.strip():
                updates["notes"] = f"{new_entry}\n---\n{existing}"
            else:
                updates["notes"] = new_entry

        if followup_date:
            updates["followup"] = followup_date

        if new_status:
            updates["status"] = new_status
        elif call_status and not new_status:
            # Auto-promote New → Contacted on first call
            current_status = row[COL_LS["status"]] if len(row) > COL_LS["status"] else ""
            if current_status.lower() == "new":
                updates["status"] = "Contacted"

        return self.update_lead(lead_id, updates)

    def get_pipeline_stats(self) -> dict:
        """Aggregated counts for KPIs."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        stats = {
            "total": len(self._cache),
            "queue": 0,
            "followups_due": 0,
            "by_status": {},
            "by_call_status": {},
            "by_vertical": {},
        }
        terminal = {"won", "not interested", "not a fit", "bad data"}

        for row in self._cache:
            d = self._row_to_dict(list(row))
            # Pipeline status counts
            status = (d.get("status") or "New").strip()
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

            # Call status counts
            cs = (d.get("call_status") or "Never Called").strip()
            stats["by_call_status"][cs] = stats["by_call_status"].get(cs, 0) + 1

            # Vertical counts
            vert = (d.get("vertical") or "Unknown").strip()
            stats["by_vertical"][vert] = stats["by_vertical"].get(vert, 0) + 1

            # Queue / followup counting
            if status.lower() not in terminal:
                call_status = (d.get("call_status") or "").strip()
                followup = (d.get("followup") or "").strip()
                if not call_status:
                    stats["queue"] += 1
                elif followup and followup <= today_str:
                    stats["queue"] += 1
                    stats["followups_due"] += 1

        return stats
