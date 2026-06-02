"""
LinkedIn Outbound — Google Sheets CRM
=======================================
Manages the "LinkedIn Outbound" tab in the shared CRM spreadsheet.
"""

import gspread
from gspread.exceptions import APIError
from gspread.cell import Cell
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from typing import Optional
import time
import structlog

from src.linkedin.config import (
    LINKEDIN_CRM_HEADERS,
    LINKEDIN_SHEET_NAME,
    LCOL,
    SAFETY_LIMITS,
    DM1_VARIANTS,
)

logger = structlog.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_API_CALL_INTERVAL = 1.5


class LinkedInSheetsCRM:
    """Google Sheets CRM for the LinkedIn Outbound pipeline."""

    def __init__(self, credentials_file: str, spreadsheet_id: str):
        self.spreadsheet_id = spreadsheet_id
        self._last_api_call = 0
        self._cache = []

        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.spreadsheet = self._api_call(self.client.open_by_key, spreadsheet_id)
        self.sheet = self._get_or_create_sheet(LINKEDIN_SHEET_NAME)
        self._ensure_headers()
        self._refresh_cache()

        logger.info("LinkedIn CRM initialized",
                     spreadsheet_id=spreadsheet_id,
                     sheet=LINKEDIN_SHEET_NAME,
                     rows=len(self._cache))

    def _throttle(self):
        elapsed = time.time() - self._last_api_call
        if elapsed < _API_CALL_INTERVAL:
            time.sleep(_API_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    def _api_call(self, func, *args, **kwargs):
        self._throttle()
        for attempt in range(5):
            try:
                return func(*args, **kwargs)
            except APIError as e:
                if e.response.status_code in (429, 500, 503) and attempt < 4:
                    wait = (attempt + 1) * 5
                    logger.warning("Sheets rate limit, retrying", wait=wait)
                    time.sleep(wait)
                else:
                    raise

    def _get_or_create_sheet(self, name: str):
        try:
            return self._api_call(self.spreadsheet.worksheet, name)
        except gspread.WorksheetNotFound:
            logger.info("Creating LinkedIn Outbound sheet")
            return self._api_call(
                self.spreadsheet.add_worksheet,
                title=name, rows=2000, cols=len(LINKEDIN_CRM_HEADERS)
            )

    def _ensure_headers(self):
        current = self._api_call(self.sheet.row_values, 1)
        if not current:
            self._api_call(self.sheet.update, "A1", [LINKEDIN_CRM_HEADERS])
            logger.info("Headers written to LinkedIn Outbound sheet")

    def _refresh_cache(self):
        all_values = self._api_call(self.sheet.get_all_values)
        self._cache = all_values[1:] if len(all_values) > 1 else []
        logger.info("LinkedIn CRM cache refreshed", rows=len(self._cache))

    def _get_cell(self, row: list, col_key: str) -> str:
        idx = LCOL.get(col_key)
        if idx is not None and idx < len(row):
            return row[idx]
        return ""

    def _row_to_dict(self, row: list) -> dict:
        while len(row) < len(LINKEDIN_CRM_HEADERS):
            row.append("")
        return {key: row[idx] for key, idx in LCOL.items()}

    # -----------------------------------------------------------------------
    # Read operations (all from local cache)
    # -----------------------------------------------------------------------

    def get_prospects_by_status(self, status: str, limit: int = 50) -> list[dict]:
        """Get prospects with a specific status."""
        results = []
        for row in self._cache:
            if self._get_cell(row, "status") == status:
                results.append(self._row_to_dict(list(row)))
                if len(results) >= limit:
                    break
        return results

    def get_new_prospects(self, limit: int = 25) -> list[dict]:
        """Get prospects ready for connection requests."""
        return self.get_prospects_by_status("New", limit)

    def get_pending_connections(self) -> list[dict]:
        """Get prospects with Request Sent status (waiting for acceptance)."""
        return self.get_prospects_by_status("Request Sent", limit=500)

    def get_manual_toggles(self) -> list[dict]:
        """Get prospects where Jose manually toggled Connected? = YES."""
        results = []
        for row in self._cache:
            manual = self._get_cell(row, "connected_manual").strip().upper()
            status = self._get_cell(row, "status")
            if manual in ("YES", "TRUE", "1") and status not in ("Connected", "DM 1", "DM 2", "DM 3", "Replied"):
                results.append(self._row_to_dict(list(row)))
        return results

    def get_dm_ready(self, dm_number: int) -> list[dict]:
        """Get prospects ready for a specific DM.

        DM 1: Status = Connected, DM 1 Sent is empty
        DM 2: Status = DM 1, DM 1 Sent >= 5 days ago, DM 2 Sent is empty
        DM 3: Status = DM 2, DM 2 Sent >= 10 days ago, DM 3 Sent is empty
        """
        now = datetime.now()
        results = []

        for row in self._cache:
            d = self._row_to_dict(list(row))
            status = d.get("status", "")
            reply = d.get("reply", "")

            # Skip if already replied
            if reply:
                continue

            if dm_number == 1:
                if status == "Connected" and not d.get("dm_1_sent"):
                    results.append(d)

            elif dm_number == 2:
                if status == "DM 1" and not d.get("dm_2_sent"):
                    dm1_date = self._parse_date(d.get("dm_1_sent", ""))
                    if dm1_date and (now - dm1_date).days >= SAFETY_LIMITS["dm2_delay_days"]:
                        results.append(d)

            elif dm_number == 3:
                if status == "DM 2" and not d.get("dm_3_sent"):
                    dm2_date = self._parse_date(d.get("dm_2_sent", ""))
                    if dm2_date and (now - dm2_date).days >= SAFETY_LIMITS["dm3_delay_days"]:
                        results.append(d)

        return results

    def get_all_linkedin_urls(self) -> set:
        """Get all LinkedIn URLs already in the system (for dedup)."""
        return set(
            self._get_cell(row, "linkedin_url").lower()
            for row in self._cache
            if self._get_cell(row, "linkedin_url")
        )

    def get_week_connection_count(self) -> int:
        """Count connections sent this week (Mon-Sun)."""
        now = datetime.now()
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        count = 0
        for row in self._cache:
            sent = self._get_cell(row, "connection_sent")
            if sent:
                sent_date = self._parse_date(sent)
                if sent_date and sent_date >= week_start:
                    count += 1
        return count

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
        return None

    # -----------------------------------------------------------------------
    # Write operations
    # -----------------------------------------------------------------------

    def _generate_id(self) -> str:
        return f"LKDN-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def add_prospect(self, prospect: dict) -> Optional[str]:
        """Add a new prospect to the LinkedIn CRM."""
        # Dedup by LinkedIn URL
        url = (prospect.get("linkedin_url") or "").lower()
        if url:
            for row in self._cache:
                if self._get_cell(row, "linkedin_url").lower() == url:
                    logger.debug("Duplicate LinkedIn URL", url=url)
                    return None

        # Dedup by company
        company = (prospect.get("company") or "").lower()
        if company:
            for row in self._cache:
                if self._get_cell(row, "company").lower() == company:
                    logger.debug("Duplicate company", company=company)
                    return None

        lead_id = self._generate_id()
        row = [""] * len(LINKEDIN_CRM_HEADERS)
        row[LCOL["id"]] = lead_id
        row[LCOL["company"]] = prospect.get("company", "")
        row[LCOL["contact_name"]] = prospect.get("contact_name", "")
        row[LCOL["title"]] = prospect.get("title", "")
        row[LCOL["linkedin_url"]] = prospect.get("linkedin_url", "")
        row[LCOL["company_linkedin"]] = prospect.get("company_linkedin", "")
        row[LCOL["job_hiring"]] = prospect.get("job_hiring", "")
        row[LCOL["country"]] = prospect.get("country", "")
        row[LCOL["employee_count"]] = str(prospect.get("employee_count", ""))
        row[LCOL["industry"]] = prospect.get("industry", "")
        row[LCOL["status"]] = "New"
        row[LCOL["connected_manual"]] = "FALSE"
        row[LCOL["source"]] = prospect.get("source", "apify_bulk_scrape")
        row[LCOL["description_snippet"]] = prospect.get("description_snippet", "")

        self._api_call(self.sheet.append_row, row, table_range="A1")
        self._cache.append(row)
        logger.info("Prospect added", lead_id=lead_id, company=prospect.get("company"))
        return lead_id

    def update_prospect(self, lead_id: str, updates: dict) -> bool:
        """Update a prospect's fields."""
        for i, row in enumerate(self._cache):
            if self._get_cell(row, "id") == lead_id:
                row_num = i + 2  # 1-indexed, skip header
                cells = []
                for field, value in updates.items():
                    col_idx = LCOL.get(field.lower().replace(" ", "_").replace("?", "").replace("(", "").replace(")", ""))
                    if col_idx is not None:
                        cells.append(Cell(row=row_num, col=col_idx + 1, value=str(value)))
                        while len(self._cache[i]) <= col_idx:
                            self._cache[i].append("")
                        self._cache[i][col_idx] = str(value)

                if cells:
                    self._api_call(self.sheet.update_cells, cells)
                    logger.info("Prospect updated", lead_id=lead_id, fields=list(updates.keys()))
                return True

        logger.warning("Prospect not found", lead_id=lead_id)
        return False

    def mark_connection_sent(self, lead_id: str) -> bool:
        return self.update_prospect(lead_id, {
            "status": "Request Sent",
            "connection_sent": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    def mark_connected(self, lead_id: str) -> bool:
        return self.update_prospect(lead_id, {
            "status": "Connected",
            "connection_accepted": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    def mark_dm_sent(self, lead_id: str, dm_number: int, variant: str = "") -> bool:
        """Mark a DM as sent. For DM 1, also records the A/B/C variant used."""
        field = f"dm_{dm_number}_sent"
        status = f"DM {dm_number}"
        updates = {
            "status": status,
            field: datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if dm_number == 1 and variant:
            updates["dm_1_variant"] = variant
        return self.update_prospect(lead_id, updates)

    def mark_replied(self, lead_id: str, reply_text: str) -> bool:
        return self.update_prospect(lead_id, {
            "status": "Replied",
            "reply": reply_text[:500],
            "reply_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    # -----------------------------------------------------------------------
    # Variant tracking (A/B/C testing for DM 1)
    # -----------------------------------------------------------------------

    def get_variant_counts(self) -> dict:
        """Count how many times each DM 1 variant (A/B/C) has been used."""
        counts = {v: 0 for v in DM1_VARIANTS}
        for row in self._cache:
            variant = self._get_cell(row, "dm_1_variant").strip().upper()
            if variant in counts:
                counts[variant] += 1
        return counts

    def get_variant_reply_rates(self) -> dict:
        """Calculate reply rates per DM 1 variant.

        Returns dict like {"A": {"sent": 10, "replied": 3, "rate": 0.30}, ...}
        """
        stats = {v: {"sent": 0, "replied": 0, "rate": 0.0} for v in DM1_VARIANTS}
        for row in self._cache:
            variant = self._get_cell(row, "dm_1_variant").strip().upper()
            if variant not in stats:
                continue
            stats[variant]["sent"] += 1
            reply = self._get_cell(row, "reply").strip()
            if reply:
                stats[variant]["replied"] += 1

        for v in DM1_VARIANTS:
            sent = stats[v]["sent"]
            if sent > 0:
                stats[v]["rate"] = round(stats[v]["replied"] / sent, 2)

        return stats

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = {"total": len(self._cache)}
        for status in ("New", "Request Sent", "Connected", "DM 1", "DM 2", "DM 3", "Replied", "No Reply"):
            stats[status.lower().replace(" ", "_")] = sum(
                1 for row in self._cache if self._get_cell(row, "status") == status
            )
        stats["week_connections"] = self.get_week_connection_count()
        stats["variant_counts"] = self.get_variant_counts()
        stats["variant_reply_rates"] = self.get_variant_reply_rates()
        return stats
