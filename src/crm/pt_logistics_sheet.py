"""
PT Logistics Google Sheets CRM adapter.

This adapter matches the active "PT Logistics" tab used by the dashboard.
It keeps the sheet schema intact and maps the existing headers to stable
Python keys for call logging and manual email follow-up tracking.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
import time

import gspread
import structlog
from gspread.cell import Cell
from gspread.exceptions import APIError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.crm.callback_calendar import CallbackCalendar
from src.crm.google_credentials import load_google_credentials

logger = structlog.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

_API_CALL_INTERVAL = 1.5
ACTIVITY_LOG_SHEET_NAME = "Dashboard Activity"
STAGE_EVENT_SHEET_NAME = "Lead Stage Events"

PT_LOGISTICS_HEADERS = [
    "ID",
    "Company",
    "Contact",
    "Phone",
    "Email",
    "Stage",
    "notes",
    "Last Touch Type",
    "What Happened",
    "Due",
    "Due Time",
    "Calendar Event ID",
    "Initial Email Sent",
    "Outreach FU1 Sent",
    "Outreach FU2 Sent",
    "Outreach FU3 Sent",
    "Outreach Reactivation Sent",
    "Proposal Sent",
    "Proposal Status",
    "Proposal FU1 Sent",
    "Proposal FU2 Sent",
    "Proposal FU3 Sent",
    "Proposal Reactivation Sent",
    "Proposal Next Action",
    "Proposal Next Action Due",
    "Proposal Outcome",
    "Proposal Lost Reason",
    "Proposal Value",
    "Proposal Probability",
    "Forecast Category",
    "FU1 Sent",
    "FU2 Sent",
    "FU3 Sent",
    "Reactivation Sent",
    "Meeting Date",
    "Outcome",
    "Priority",
    "Website",
    "City",
    "Region",
    "Dashboard Touched",
]

ACTIVITY_LOG_HEADERS = [
    "Timestamp",
    "Date",
    "Event Type",
    "Lead Key",
    "Row Number",
    "Company",
    "Stage Before",
    "Stage After",
    "Due Before",
    "Due After",
    "Call Status",
    "Email Task",
    "New Lead Impacted",
    "Full Lead Impacted",
    "Notes",
    "Proposal Status Before",
    "Proposal Status After",
]

STAGE_EVENT_HEADERS = [
    "Timestamp",
    "Date",
    "Lead Key",
    "Row Number",
    "Company",
    "Event Type",
    "Stage Before",
    "Stage After",
    "Proposal Status Before",
    "Proposal Status After",
    "Call Status",
    "Email Task",
    "Notes",
]

FIELD_ALIASES = {
    "id": "ID",
    "company": "Company",
    "contact": "Contact",
    "phone": "Phone",
    "email": "Email",
    "stage": "Stage",
    "notes": "notes",
    "last_touch_type": "Last Touch Type",
    "what_happened": "What Happened",
    "due": "Due",
    "due_time": "Due Time",
    "calendar_event_id": "Calendar Event ID",
    "initial_email_sent": "Initial Email Sent",
    "outreach_fu1_sent": "Outreach FU1 Sent",
    "outreach_fu2_sent": "Outreach FU2 Sent",
    "outreach_fu3_sent": "Outreach FU3 Sent",
    "outreach_reactivation_sent": "Outreach Reactivation Sent",
    "proposal_sent": "Proposal Sent",
    "proposal_status": "Proposal Status",
    "proposal_fu1_sent": "Proposal FU1 Sent",
    "proposal_fu2_sent": "Proposal FU2 Sent",
    "proposal_fu3_sent": "Proposal FU3 Sent",
    "proposal_reactivation_sent": "Proposal Reactivation Sent",
    "proposal_next_action": "Proposal Next Action",
    "proposal_next_action_due": "Proposal Next Action Due",
    "proposal_outcome": "Proposal Outcome",
    "proposal_lost_reason": "Proposal Lost Reason",
    "proposal_value": "Proposal Value",
    "proposal_probability": "Proposal Probability",
    "forecast_category": "Forecast Category",
    "fu1_sent": "FU1 Sent",
    "fu2_sent": "FU2 Sent",
    "fu3_sent": "FU3 Sent",
    "reactivation_sent": "Reactivation Sent",
    "meeting_date": "Meeting Date",
    "outcome": "Outcome",
    "priority": "Priority",
    "website": "Website",
    "city": "City",
    "region": "Region",
    "dashboard_touched": "Dashboard Touched",
}

DATE_FIELDS = {
    "due",
    "initial_email_sent",
    "outreach_fu1_sent",
    "outreach_fu2_sent",
    "outreach_fu3_sent",
    "outreach_reactivation_sent",
    "proposal_sent",
    "proposal_fu1_sent",
    "proposal_fu2_sent",
    "proposal_fu3_sent",
    "proposal_reactivation_sent",
    "proposal_next_action_due",
    "fu1_sent",
    "fu2_sent",
    "fu3_sent",
    "reactivation_sent",
    "meeting_date",
    "dashboard_touched",
}

TERMINAL_STAGES = {"lost", "not a fit"}
PROPOSAL_STAGES = {"send proposal", "proposal requested", "proposal to send"}
EMAIL_TASK_STAGES = {"send email", "email sent"}

OUTREACH_FOLLOWUP_RULES = [
    {
        "type": "Initial",
        "source_field": "dashboard_touched",
        "target_field": "initial_email_sent",
        "legacy_target_fields": ["proposal_sent", "fu1_sent"],
        "days_after": 0,
        "label": "Initial outreach email",
    },
    {
        "type": "Outreach FU1",
        "source_field": "initial_email_sent",
        "target_field": "outreach_fu1_sent",
        "legacy_source_fields": ["proposal_sent"],
        "legacy_target_fields": ["fu1_sent"],
        "days_after": 2,
        "label": "First outreach follow-up",
    },
    {
        "type": "Outreach FU2",
        "source_field": "outreach_fu1_sent",
        "target_field": "outreach_fu2_sent",
        "legacy_source_fields": ["fu1_sent"],
        "legacy_target_fields": ["fu2_sent"],
        "days_after": 3,
        "label": "Second outreach follow-up",
    },
    {
        "type": "Outreach FU3",
        "source_field": "outreach_fu2_sent",
        "target_field": "outreach_fu3_sent",
        "legacy_source_fields": ["fu2_sent"],
        "legacy_target_fields": ["fu3_sent"],
        "days_after": 5,
        "label": "Third outreach follow-up",
    },
    {
        "type": "Outreach Reactivation",
        "source_field": "outreach_fu3_sent",
        "target_field": "outreach_reactivation_sent",
        "legacy_source_fields": ["fu3_sent"],
        "legacy_target_fields": ["reactivation_sent"],
        "days_after": 30,
        "label": "Outreach reactivation email",
    },
]

PROPOSAL_FOLLOWUP_RULES = [
    {
        "type": "Proposal FU1",
        "source_field": "proposal_sent",
        "target_field": "proposal_fu1_sent",
        "legacy_target_fields": ["fu1_sent"],
        "days_after": 2,
        "label": "First proposal follow-up",
        "next_status": "Follow-up 1",
    },
    {
        "type": "Proposal FU2",
        "source_field": "proposal_fu1_sent",
        "target_field": "proposal_fu2_sent",
        "legacy_source_fields": ["fu1_sent"],
        "legacy_target_fields": ["fu2_sent"],
        "days_after": 3,
        "label": "Second proposal follow-up",
        "next_status": "Follow-up 2",
    },
    {
        "type": "Proposal FU3",
        "source_field": "proposal_fu2_sent",
        "target_field": "proposal_fu3_sent",
        "legacy_source_fields": ["fu2_sent"],
        "legacy_target_fields": ["fu3_sent"],
        "days_after": 5,
        "label": "Third proposal follow-up",
        "next_status": "Follow-up 3",
    },
    {
        "type": "Proposal Reactivation",
        "source_field": "proposal_fu3_sent",
        "target_field": "proposal_reactivation_sent",
        "legacy_source_fields": ["fu3_sent"],
        "legacy_target_fields": ["reactivation_sent"],
        "days_after": 30,
        "label": "Proposal reactivation",
        "next_status": "Reactivation",
    },
]


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


def parse_sheet_date(value: str) -> Optional[date]:
    """Parse dates commonly found in the PT Logistics sheet."""
    value = (value or "").strip()
    if not value:
        return None

    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.split()[0], fmt).date()
        except ValueError:
            continue
    return None


def format_sheet_date(value: str | date | datetime) -> str:
    """Format dates for the existing sheet convention: YYYY/MM/DD."""
    if isinstance(value, datetime):
        return value.date().strftime("%Y/%m/%d")
    if isinstance(value, date):
        return value.strftime("%Y/%m/%d")

    parsed = parse_sheet_date(value)
    return parsed.strftime("%Y/%m/%d") if parsed else (value or "")


def normalize_time(value: str) -> str:
    """Normalize a dashboard time input to HH:MM."""
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return value


def is_filled(value: str) -> bool:
    """Treat dates and X/x markers as completed follow-up fields."""
    return bool((value or "").strip())


class PTLogisticsCRM:
    """Google Sheets CRM wrapper for the active PT Logistics workflow."""

    def __init__(
        self,
        credentials_file: str,
        spreadsheet_id: str,
        sheet_name: str = "PT Logistics",
        callback_calendar_id: str = "",
        app_timezone: str = "Europe/Lisbon",
        callback_credentials_file: str = "",
    ):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._last_api_call = 0.0
        self._cache: list[list[str]] = []
        self.headers: list[str] = []
        self._columns: dict[str, int] = {}
        self.last_warning = ""
        self.callback_calendar = CallbackCalendar(
            credentials_file=callback_credentials_file or credentials_file,
            calendar_id=callback_calendar_id,
            timezone=app_timezone,
        )

        creds = load_google_credentials(credentials_file, SCOPES)
        self.client = gspread.authorize(creds)
        self.spreadsheet = self._api_call(self.client.open_by_key, spreadsheet_id)
        self.sheet = self._get_or_create_sheet(sheet_name)
        self._ensure_headers()
        self.activity_sheet = self._get_or_create_activity_sheet()
        self._ensure_activity_headers()
        self.stage_event_sheet = self._get_or_create_stage_event_sheet()
        self._ensure_stage_event_headers()
        self._refresh_cache()

        logger.info("PTLogisticsCRM initialized", spreadsheet_id=spreadsheet_id, sheet=sheet_name)

    def consume_warning(self) -> str:
        warning = self.last_warning
        self.last_warning = ""
        return warning

    def _throttle(self):
        elapsed = time.time() - self._last_api_call
        if elapsed < _API_CALL_INTERVAL:
            time.sleep(_API_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    @_sheets_retry
    def _api_call(self, func, *args, **kwargs):
        self._throttle()
        return func(*args, **kwargs)

    def _get_or_create_sheet(self, sheet_name: str):
        try:
            return self._api_call(self.spreadsheet.worksheet, sheet_name)
        except gspread.WorksheetNotFound:
            logger.info("Creating PT Logistics sheet", sheet_name=sheet_name)
            return self._api_call(
                self.spreadsheet.add_worksheet,
                title=sheet_name,
                rows=1500,
                cols=len(PT_LOGISTICS_HEADERS),
            )

    def _get_or_create_activity_sheet(self):
        try:
            return self._api_call(self.spreadsheet.worksheet, ACTIVITY_LOG_SHEET_NAME)
        except gspread.WorksheetNotFound:
            logger.info("Creating PT Logistics activity sheet", sheet_name=ACTIVITY_LOG_SHEET_NAME)
            return self._api_call(
                self.spreadsheet.add_worksheet,
                title=ACTIVITY_LOG_SHEET_NAME,
                rows=3000,
                cols=len(ACTIVITY_LOG_HEADERS),
            )

    def _get_or_create_stage_event_sheet(self):
        try:
            return self._api_call(self.spreadsheet.worksheet, STAGE_EVENT_SHEET_NAME)
        except gspread.WorksheetNotFound:
            logger.info("Creating PT Logistics stage event sheet", sheet_name=STAGE_EVENT_SHEET_NAME)
            return self._api_call(
                self.spreadsheet.add_worksheet,
                title=STAGE_EVENT_SHEET_NAME,
                rows=3000,
                cols=len(STAGE_EVENT_HEADERS),
            )

    def _ensure_headers(self):
        current_headers = self._api_call(self.sheet.row_values, 1)
        if not current_headers:
            self._api_call(self.sheet.update, "A1", [PT_LOGISTICS_HEADERS])
            current_headers = PT_LOGISTICS_HEADERS
            logger.info("PT Logistics headers written to empty sheet")

        missing_headers = [header for header in PT_LOGISTICS_HEADERS if header not in current_headers]
        if missing_headers:
            start_col = len(current_headers) + 1
            required_cols = len(current_headers) + len(missing_headers)
            current_cols = getattr(self.sheet, "col_count", len(current_headers))
            if required_cols > current_cols:
                self._api_call(self.sheet.add_cols, required_cols - current_cols)

            cells = [
                Cell(row=1, col=start_col + idx, value=header)
                for idx, header in enumerate(missing_headers)
            ]
            self._api_call(self.sheet.update_cells, cells)
            current_headers = [*current_headers, *missing_headers]
            logger.info("PT Logistics headers extended", headers=missing_headers)

        self.headers = current_headers
        header_index = {h.strip().lower(): i for i, h in enumerate(self.headers)}
        self._columns = {}
        for field, header in FIELD_ALIASES.items():
            idx = header_index.get(header.lower())
            if idx is not None:
                self._columns[field] = idx

    def _ensure_activity_headers(self):
        current_headers = self._api_call(self.activity_sheet.row_values, 1)
        if not current_headers:
            self._api_call(self.activity_sheet.update, "A1", [ACTIVITY_LOG_HEADERS])
            return

        missing_headers = [header for header in ACTIVITY_LOG_HEADERS if header not in current_headers]
        if missing_headers:
            start_col = len(current_headers) + 1
            required_cols = len(current_headers) + len(missing_headers)
            current_cols = getattr(self.activity_sheet, "col_count", len(current_headers))
            if required_cols > current_cols:
                self._api_call(self.activity_sheet.add_cols, required_cols - current_cols)
            cells = [
                Cell(row=1, col=start_col + idx, value=header)
                for idx, header in enumerate(missing_headers)
            ]
            self._api_call(self.activity_sheet.update_cells, cells)

    def _ensure_stage_event_headers(self):
        current_headers = self._api_call(self.stage_event_sheet.row_values, 1)
        if not current_headers:
            self._api_call(self.stage_event_sheet.update, "A1", [STAGE_EVENT_HEADERS])
            return

        missing_headers = [header for header in STAGE_EVENT_HEADERS if header not in current_headers]
        if missing_headers:
            start_col = len(current_headers) + 1
            required_cols = len(current_headers) + len(missing_headers)
            current_cols = getattr(self.stage_event_sheet, "col_count", len(current_headers))
            if required_cols > current_cols:
                self._api_call(self.stage_event_sheet.add_cols, required_cols - current_cols)
            cells = [
                Cell(row=1, col=start_col + idx, value=header)
                for idx, header in enumerate(missing_headers)
            ]
            self._api_call(self.stage_event_sheet.update_cells, cells)

    def _refresh_cache(self):
        all_values = self._api_call(self.sheet.get_all_values)
        self.headers = all_values[0] if all_values else []
        self._ensure_headers()
        self._cache = all_values[1:] if len(all_values) > 1 else []
        logger.info("PT Logistics cache refreshed", rows=len(self._cache))

    def _value(self, row: list[str], field: str) -> str:
        idx = self._columns.get(field)
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    def _proposal_legacy_date(self, lead: dict, field: str) -> Optional[date]:
        proposal_date = parse_sheet_date(lead.get("proposal_sent", ""))
        value_date = parse_sheet_date(lead.get(field, ""))
        if proposal_date and value_date and value_date >= proposal_date:
            return value_date
        return None

    def _proposal_rule_source_date(self, lead: dict, rule: dict) -> Optional[date]:
        source_date = parse_sheet_date(lead.get(rule["source_field"], ""))
        if source_date:
            return source_date
        for field in rule.get("legacy_source_fields", []):
            legacy_date = self._proposal_legacy_date(lead, field)
            if legacy_date:
                return legacy_date
        return None

    def _proposal_rule_target_filled(self, lead: dict, rule: dict) -> bool:
        if is_filled(lead.get(rule["target_field"], "")):
            return True
        return any(
            self._proposal_legacy_date(lead, field)
            for field in rule.get("legacy_target_fields", [])
        )

    def _proposal_status(self, lead: dict) -> str:
        explicit = (lead.get("proposal_status") or "").strip()
        if explicit:
            return explicit

        stage = (lead.get("stage") or "").strip()
        stage_lower = stage.lower()
        if stage_lower in PROPOSAL_STAGES:
            return "Requested"
        if not self._has_actual_proposal(lead):
            return ""
        if stage_lower == "lost":
            return "Lost"
        if stage_lower == "not a fit":
            return "Not a Fit"
        if stage_lower == "meeting booked":
            return "Meeting Booked"
        if is_filled(lead.get("proposal_reactivation_sent", "")) or self._proposal_legacy_date(lead, "reactivation_sent"):
            return "Reactivation"
        if is_filled(lead.get("proposal_fu3_sent", "")) or self._proposal_legacy_date(lead, "fu3_sent"):
            return "Follow-up 3"
        if is_filled(lead.get("proposal_fu2_sent", "")) or self._proposal_legacy_date(lead, "fu2_sent"):
            return "Follow-up 2"
        if is_filled(lead.get("proposal_fu1_sent", "")) or self._proposal_legacy_date(lead, "fu1_sent"):
            return "Follow-up 1"
        return "Sent"

    def _has_actual_proposal(self, lead: dict) -> bool:
        if not is_filled(lead.get("proposal_sent", "")):
            return False

        stage = (lead.get("stage") or "").strip().lower()
        last_touch = (lead.get("last_touch_type") or "").strip().lower()
        if stage in {"proposal sent", "meeting booked"} or "proposal" in last_touch:
            return True
        if is_filled(lead.get("proposal_status", "")):
            return True
        return any(
            is_filled(lead.get(field, ""))
            for field in (
                "proposal_fu1_sent",
                "proposal_fu2_sent",
                "proposal_fu3_sent",
                "proposal_reactivation_sent",
                "proposal_next_action",
                "proposal_next_action_due",
                "proposal_outcome",
                "proposal_lost_reason",
                "proposal_value",
                "proposal_probability",
                "forecast_category",
            )
        )

    def _is_proposal_open(self, lead: dict) -> bool:
        if not self._has_actual_proposal(lead):
            return False
        status = self._proposal_status(lead).strip().lower()
        stage = (lead.get("stage") or "").strip().lower()
        return status not in {"lost", "not a fit", "won", "meeting booked"} and stage not in TERMINAL_STAGES

    def _has_outreach_context(self, lead: dict) -> bool:
        stage = (lead.get("stage") or "").strip().lower()
        if stage in EMAIL_TASK_STAGES:
            return True
        return any(
            is_filled(lead.get(field, ""))
            for field in (
                "initial_email_sent",
                "outreach_fu1_sent",
                "outreach_fu2_sent",
                "outreach_fu3_sent",
                "outreach_reactivation_sent",
                "fu1_sent",
                "fu2_sent",
                "fu3_sent",
                "reactivation_sent",
            )
        )

    def _field_date(self, lead: dict, field: str) -> Optional[date]:
        return parse_sheet_date(lead.get(field, ""))

    def _first_filled_date(self, lead: dict, *fields: str) -> Optional[date]:
        for field in fields:
            if not field:
                continue
            parsed = self._field_date(lead, field)
            if parsed:
                return parsed
        return None

    def _rule_fields(self, rule: dict, field_name: str, legacy_name: str, legacy_list_name: str) -> list[str]:
        fields = [rule.get(field_name, "")]
        fields.extend(rule.get(legacy_list_name, []))
        legacy_field = rule.get(legacy_name, "")
        if legacy_field:
            fields.append(legacy_field)
        return [field for field in fields if field]

    def _rule_target_filled(self, lead: dict, rule: dict) -> bool:
        return any(
            is_filled(lead.get(field, ""))
            for field in self._rule_fields(rule, "target_field", "legacy_target_field", "legacy_target_fields")
        )

    def _row_to_dict(self, row: list[str], row_number: int | None = None) -> dict:
        result = {}
        for field in FIELD_ALIASES:
            result[field] = self._value(row, field)
        result["due_time"] = normalize_time(result.get("due_time", ""))
        result["row_number"] = row_number or ""
        result["key"] = result.get("id") or f"row-{row_number}" if row_number else result.get("id", "")

        for field in DATE_FIELDS:
            parsed = parse_sheet_date(result.get(field, ""))
            result[f"{field}_iso"] = parsed.isoformat() if parsed else ""

        result["has_actual_proposal"] = self._has_actual_proposal(result)
        result["proposal_status_effective"] = self._proposal_status(result)

        return result

    def _write_field(self, row_num: int, field: str, value: str):
        col_idx = self._columns.get(field)
        cache_idx = row_num - 2
        if col_idx is None or cache_idx < 0 or cache_idx >= len(self._cache):
            return

        self._api_call(self.sheet.update_cells, [Cell(row=row_num, col=col_idx + 1, value=value)])
        while len(self._cache[cache_idx]) <= col_idx:
            self._cache[cache_idx].append("")
        self._cache[cache_idx][col_idx] = value

    def _callback_description(self, lead: dict) -> str:
        lines = [
            f"Company: {lead.get('company') or '-'}",
            f"Contact: {lead.get('contact') or '-'}",
            f"Phone: {lead.get('phone') or '-'}",
            f"Email: {lead.get('email') or '-'}",
            f"Stage: {lead.get('stage') or '-'}",
        ]
        what_happened = (lead.get("what_happened") or "").strip()
        if what_happened:
            lines.append(f"What happened: {what_happened}")
        note = (lead.get("notes") or "").strip().split("\n---\n")[0].strip()
        if note:
            lines.append(f"Latest note: {note}")
        lines.append("")
        lines.append("Created from the PT Logistics dashboard callback workflow.")
        return "\n".join(lines)

    def _sync_callback_calendar(self, row_num: int, before: dict, after: dict):
        due = after.get("due_iso") or ""
        due_time = normalize_time(after.get("due_time", ""))
        event_id = (after.get("calendar_event_id") or "").strip()

        if due and due_time:
            result = self.callback_calendar.upsert_event(
                event_id=event_id,
                due_date=due,
                due_time=due_time,
                title=f"Call: {after.get('company') or 'Lead'}",
                description=self._callback_description(after),
            )
            if result.warning:
                self.last_warning = result.warning
            if result.ok and result.event_id != event_id:
                self._write_field(row_num, "calendar_event_id", result.event_id)
            return

        if event_id:
            result = self.callback_calendar.delete_event(event_id)
            if result.warning:
                self.last_warning = result.warning
            if result.ok and result.event_id != event_id:
                self._write_field(row_num, "calendar_event_id", result.event_id)

    def _find_by_id(self, lead_id: str) -> Optional[tuple[int, list[str]]]:
        lead_id_lower = (lead_id or "").lower()
        for i, row in enumerate(self._cache):
            if self._value(row, "id").lower() == lead_id_lower:
                return i + 2, row
        return None

    def _find_by_reference(self, lead_id: str = "", row_number: int | str = "") -> Optional[tuple[int, list[str]]]:
        if lead_id:
            match = self._find_by_id(lead_id)
            if match:
                return match

        try:
            row_num = int(row_number)
        except (TypeError, ValueError):
            return None

        cache_idx = row_num - 2
        if cache_idx < 0 or cache_idx >= len(self._cache):
            return None
        return row_num, self._cache[cache_idx]

    def get_all_leads(self) -> list[dict]:
        return [self._row_to_dict(list(row), i + 2) for i, row in enumerate(self._cache)]

    def get_impacted_leads(self, today: date) -> list[dict]:
        leads = []
        for i, row in enumerate(self._cache):
            lead = self._row_to_dict(list(row), i + 2)
            touched = parse_sheet_date(lead.get("dashboard_touched", ""))
            if touched == today:
                leads.append(lead)

        return sorted(
            leads,
            key=lambda lead: (
                (lead.get("stage") or "").lower(),
                (lead.get("company") or "").lower(),
            ),
        )

    def get_call_leads(self, view: str, today: date) -> list[dict]:
        leads = []
        for i, row in enumerate(self._cache):
            d = self._row_to_dict(list(row), i + 2)
            stage = (d.get("stage") or "").strip().lower()
            if stage in TERMINAL_STAGES:
                continue

            due = parse_sheet_date(d.get("due", ""))
            if view == "today" and due == today:
                leads.append(d)
            elif view == "overdue" and due and due < today:
                leads.append(d)
            elif view == "due" and due and due <= today:
                leads.append(d)
            elif view == "upcoming" and due and due > today:
                leads.append(d)
            elif view == "all":
                leads.append(d)

        return sorted(
            leads,
            key=lambda lead: (
                lead.get("due_iso") or "9999-12-31",
                normalize_time(lead.get("due_time", "")) or "99:99",
                (lead.get("company") or "").lower(),
            ),
        )

    def get_email_followups(
        self,
        today: date,
        view: str = "due",
        include_upcoming: bool = False,
    ) -> list[dict]:
        return self.get_outreach_followups(today, view=view, include_upcoming=include_upcoming)

    def get_outreach_followups(
        self,
        today: date,
        view: str = "due",
        include_upcoming: bool = False,
    ) -> list[dict]:
        view = view if view in {"today", "overdue", "due", "upcoming", "all"} else "due"
        tasks = []
        for i, row in enumerate(self._cache):
            lead = self._row_to_dict(list(row), i + 2)
            stage = (lead.get("stage") or "").strip().lower()
            if stage in TERMINAL_STAGES:
                continue
            if self._has_actual_proposal(lead):
                continue
            if not self._has_outreach_context(lead):
                continue

            for index, rule in enumerate(OUTREACH_FOLLOWUP_RULES):
                if index == 0:
                    if stage not in EMAIL_TASK_STAGES or self._rule_target_filled(lead, rule):
                        continue
                    due_date = (
                        parse_sheet_date(lead.get("due", ""))
                        or parse_sheet_date(lead.get("dashboard_touched", ""))
                        or (today if stage == "send email" else None)
                    )
                    if due_date and self._email_followup_in_view(due_date, today, view, include_upcoming):
                        tasks.append({
                            **lead,
                            "task_type": rule["type"],
                            "task_workflow": "outreach",
                            "task_label": "Send initial outreach email" if stage == "send email" else rule["label"],
                            "task_due": due_date.strftime("%Y/%m/%d"),
                            "task_due_iso": due_date.isoformat(),
                            "task_overdue_days": max((today - due_date).days, 0),
                            "source_field": rule["source_field"],
                            "target_field": rule["target_field"],
                            "based_on": "Email stage",
                        })
                    break

                source_date = self._first_filled_date(
                    lead,
                    *self._rule_fields(rule, "source_field", "legacy_source_field", "legacy_source_fields"),
                )

                if not source_date or self._rule_target_filled(lead, rule):
                    continue

                due_date = source_date + timedelta(days=rule["days_after"])
                if self._email_followup_in_view(due_date, today, view, include_upcoming):
                    tasks.append({
                        **lead,
                        "task_type": rule["type"],
                        "task_workflow": "outreach",
                        "task_label": rule["label"],
                        "task_due": due_date.strftime("%Y/%m/%d"),
                        "task_due_iso": due_date.isoformat(),
                        "task_overdue_days": max((today - due_date).days, 0),
                        "source_field": rule["source_field"],
                        "target_field": rule["target_field"],
                        "based_on": format_sheet_date(source_date),
                    })
                break

        return sorted(tasks, key=lambda task: (task["task_due_iso"], task["company"].lower()))

    def get_proposal_followups(
        self,
        today: date,
        view: str = "due",
        include_upcoming: bool = False,
    ) -> list[dict]:
        view = view if view in {"today", "overdue", "due", "upcoming", "all"} else "due"
        tasks = []
        for i, row in enumerate(self._cache):
            lead = self._row_to_dict(list(row), i + 2)
            stage = (lead.get("stage") or "").strip().lower()
            if stage in TERMINAL_STAGES:
                continue
            if stage in PROPOSAL_STAGES and not is_filled(lead.get("proposal_sent", "")):
                due_date = today
                if self._email_followup_in_view(due_date, today, view, include_upcoming):
                    tasks.append({
                        **lead,
                        "task_type": "Send Proposal",
                        "task_workflow": "proposal",
                        "task_label": "Send actual proposal",
                        "task_due": "Now",
                        "task_due_iso": due_date.isoformat(),
                        "task_overdue_days": 0,
                        "source_field": "stage",
                        "target_field": "proposal_sent",
                        "based_on": "Call outcome",
                        "next_proposal_status": "Sent",
                    })
                continue
            if not self._is_proposal_open(lead):
                continue

            next_due = parse_sheet_date(lead.get("proposal_next_action_due", ""))
            if next_due:
                if self._email_followup_in_view(next_due, today, view, include_upcoming):
                    next_action = (lead.get("proposal_next_action") or "").strip()
                    tasks.append({
                        **lead,
                        "task_type": "Proposal Next Action",
                        "task_workflow": "proposal",
                        "task_label": next_action or "Proposal next action",
                        "task_due": next_due.strftime("%Y/%m/%d"),
                        "task_due_iso": next_due.isoformat(),
                        "task_overdue_days": max((today - next_due).days, 0),
                        "source_field": "proposal_next_action_due",
                        "target_field": "proposal_next_action_due",
                        "based_on": next_action or "Manual next action",
                        "next_proposal_status": self._proposal_status(lead),
                    })
                continue

            for rule in PROPOSAL_FOLLOWUP_RULES:
                source_date = self._proposal_rule_source_date(lead, rule)
                if not source_date or self._proposal_rule_target_filled(lead, rule):
                    continue

                due_date = source_date + timedelta(days=rule["days_after"])
                if self._email_followup_in_view(due_date, today, view, include_upcoming):
                    tasks.append({
                        **lead,
                        "task_type": rule["type"],
                        "task_workflow": "proposal",
                        "task_label": rule["label"],
                        "task_due": due_date.strftime("%Y/%m/%d"),
                        "task_due_iso": due_date.isoformat(),
                        "task_overdue_days": max((today - due_date).days, 0),
                        "source_field": rule["source_field"],
                        "target_field": rule["target_field"],
                        "based_on": format_sheet_date(source_date),
                        "next_proposal_status": rule["next_status"],
                    })
                break

        return sorted(tasks, key=lambda task: (task["task_due_iso"], task["company"].lower()))

    def get_proposals(self, today: date, view: str = "open") -> list[dict]:
        proposals = []
        for i, row in enumerate(self._cache):
            lead = self._row_to_dict(list(row), i + 2)
            if not self._has_actual_proposal(lead):
                continue
            proposal_date = parse_sheet_date(lead.get("proposal_sent", ""))
            if not proposal_date:
                continue

            status = self._proposal_status(lead)
            open_proposal = self._is_proposal_open(lead)
            age_days = max((today - proposal_date).days, 0)
            next_due = parse_sheet_date(lead.get("proposal_next_action_due", ""))
            proposal = {
                **lead,
                "proposal_status_effective": status,
                "proposal_age_days": age_days,
                "proposal_open": open_proposal,
                "proposal_stale": open_proposal and age_days >= 7,
                "proposal_next_action_due_iso": next_due.isoformat() if next_due else "",
                "proposal_next_action_overdue_days": max((today - next_due).days, 0) if next_due and next_due < today else 0,
            }

            if view == "stale" and not proposal["proposal_stale"]:
                continue
            if view == "open" and not open_proposal:
                continue
            if view == "closed" and open_proposal:
                continue
            proposals.append(proposal)

        return sorted(
            proposals,
            key=lambda lead: (
                0 if lead.get("proposal_stale") else 1,
                -(lead.get("proposal_age_days") or 0),
                (lead.get("company") or "").lower(),
            ),
        )

    def _money_value(self, value: str) -> float:
        cleaned = "".join(ch for ch in str(value or "") if ch.isdigit() or ch in ".,")
        if not cleaned:
            return 0.0
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    def _probability_value(self, value: str, default: float = 50.0) -> float:
        cleaned = "".join(ch for ch in str(value or "") if ch.isdigit() or ch == ".")
        if not cleaned:
            return default
        try:
            return max(0.0, min(float(cleaned), 100.0))
        except ValueError:
            return default

    def _proposal_snapshot(self, lead: dict, today: date) -> dict:
        value = self._money_value(lead.get("proposal_value", ""))
        probability = self._probability_value(lead.get("proposal_probability", ""), default=100.0 if self._proposal_status(lead).lower() == "won" else 50.0)
        sent_date = parse_sheet_date(lead.get("proposal_sent", ""))
        next_due = parse_sheet_date(lead.get("proposal_next_action_due", ""))
        return {
            "sent": lead.get("proposal_sent", ""),
            "status": self._proposal_status(lead),
            "value": round(value),
            "probability": probability,
            "weighted_value": round(value * probability / 100),
            "forecast_category": lead.get("forecast_category", "") or "Uncategorized",
            "age_days": max((today - sent_date).days, 0) if sent_date else 0,
            "next_action": lead.get("proposal_next_action", ""),
            "next_action_due": lead.get("proposal_next_action_due", ""),
            "next_action_due_iso": next_due.isoformat() if next_due else "",
            "outcome": lead.get("proposal_outcome", ""),
            "lost_reason": lead.get("proposal_lost_reason", ""),
            "open": self._is_proposal_open(lead),
        }

    def _timeline_for_lead(self, lead: dict) -> list[dict]:
        key = lead.get("key") or lead.get("id") or ""
        row_number = str(lead.get("row_number") or "")
        company = (lead.get("company") or "").strip().lower()
        timeline = []
        for row in self._activity_rows():
            row_key = row.get("Lead Key", "")
            row_number_match = str(row.get("Row Number", ""))
            row_company = (row.get("Company") or "").strip().lower()
            if not (
                (key and row_key == key)
                or (row_number and row_number_match == row_number)
                or (company and row_company == company)
            ):
                continue
            timeline.append({
                "timestamp": row.get("Timestamp", ""),
                "date": row.get("Date", ""),
                "event_type": row.get("Event Type", ""),
                "call_status": row.get("Call Status", ""),
                "email_task": row.get("Email Task", ""),
                "notes": row.get("Notes", ""),
            })
        return sorted(timeline, key=lambda event: event.get("timestamp") or event.get("date") or "", reverse=True)

    def get_account_profiles(self, today: date, stage: str = "Meeting Booked") -> list[dict]:
        target = (stage or "Meeting Booked").strip().lower()
        profiles = []
        for lead in self.get_all_leads():
            if (lead.get("stage") or "").strip().lower() != target:
                continue
            notes = [part.strip() for part in (lead.get("notes") or "").split("\n---\n") if part.strip()]
            timeline = self._timeline_for_lead(lead)
            profiles.append({
                **lead,
                "account": {
                    "company": lead.get("company", ""),
                    "contact": lead.get("contact", ""),
                    "phone": lead.get("phone", ""),
                    "email": lead.get("email", ""),
                    "website": lead.get("website", ""),
                    "city": lead.get("city", ""),
                    "region": lead.get("region", ""),
                },
                "meeting": {
                    "date": lead.get("meeting_date", ""),
                    "date_iso": lead.get("meeting_date_iso", ""),
                    "last_touch_type": lead.get("last_touch_type", ""),
                },
                "proposal": self._proposal_snapshot(lead, today),
                "notes": notes,
                "granola_notes": [note for note in notes if "granola" in note.lower()],
                "timeline": timeline,
                "emails": [event for event in timeline if (event.get("event_type") or "").lower() == "email" or event.get("email_task")],
                "meetings": [event for event in timeline if "meeting" in (event.get("event_type") or "").lower() or "meeting" in (event.get("call_status") or "").lower()],
            })
        return sorted(profiles, key=lambda profile: (profile.get("meeting", {}).get("date_iso") or "9999-12-31", profile.get("company", "").lower()))

    def get_portfolio_summary(self, today: date) -> dict:
        proposals = self.get_proposals(today, view="all")
        counts = {"open": 0, "won": 0, "lost": 0, "all": len(proposals)}
        value = {"open": 0, "won": 0, "lost": 0, "all": 0}
        forecast_by_category = {}
        open_durations = []
        followups_due = 0
        weighted_forecast = 0

        for proposal in proposals:
            snapshot = self._proposal_snapshot(proposal, today)
            status = snapshot["status"].strip().lower()
            if status in {"won", "meeting booked"}:
                bucket = "won"
                weighted = snapshot["value"]
            elif status in {"lost", "not a fit"} or (proposal.get("stage") or "").strip().lower() in TERMINAL_STAGES:
                bucket = "lost"
                weighted = 0
            else:
                bucket = "open"
                weighted = snapshot["weighted_value"]
                open_durations.append(snapshot["age_days"])
                next_due = parse_sheet_date(proposal.get("proposal_next_action_due", ""))
                if next_due and next_due <= today:
                    followups_due += 1

            counts[bucket] += 1
            value[bucket] += snapshot["value"]
            value["all"] += snapshot["value"]
            weighted_forecast += weighted
            category = snapshot["forecast_category"]
            forecast_by_category[category] = forecast_by_category.get(category, 0) + weighted

        return {
            "counts": counts,
            "value": value,
            "weighted_forecast": weighted_forecast,
            "forecast_by_category": forecast_by_category,
            "followups_due": followups_due,
            "average_open_duration_days": round(sum(open_durations) / len(open_durations)) if open_durations else 0,
            "proposals": proposals,
        }

    def get_recommendations(self, today: date, limit: int = 12) -> list[dict]:
        recommendations = []
        for proposal in self.get_proposals(today, view="open"):
            snapshot = self._proposal_snapshot(proposal, today)
            next_due = parse_sheet_date(proposal.get("proposal_next_action_due", ""))
            overdue_days = (today - next_due).days if next_due and next_due < today else 0
            if overdue_days > 0:
                recommendations.append({
                    "priority": "high",
                    "action": "Follow up on overdue proposal",
                    "reason": f"Proposal follow-up overdue by {overdue_days} days",
                    "lead_id": proposal.get("id", ""),
                    "company": proposal.get("company", ""),
                    "weighted_value": snapshot["weighted_value"],
                    "due_iso": next_due.isoformat() if next_due else "",
                })
            elif snapshot["age_days"] >= 7 and snapshot["forecast_category"].lower() in {"commit", "best case"}:
                recommendations.append({
                    "priority": "medium",
                    "action": "Refresh stale proposal",
                    "reason": f"Open {snapshot['forecast_category']} proposal is {snapshot['age_days']} days old",
                    "lead_id": proposal.get("id", ""),
                    "company": proposal.get("company", ""),
                    "weighted_value": snapshot["weighted_value"],
                    "due_iso": snapshot["next_action_due_iso"],
                })

        for profile in self.get_account_profiles(today, stage="Meeting Booked"):
            if not self._has_actual_proposal(profile):
                recommendations.append({
                    "priority": "medium",
                    "action": "Prepare meeting follow-up",
                    "reason": "Meeting booked without a tracked proposal yet",
                    "lead_id": profile.get("id", ""),
                    "company": profile.get("company", ""),
                    "weighted_value": 0,
                    "due_iso": profile.get("meeting_date_iso", ""),
                })

        priority_order = {"high": 0, "medium": 1, "low": 2}
        return sorted(
            recommendations,
            key=lambda rec: (priority_order.get(rec["priority"], 9), -rec.get("weighted_value", 0), rec.get("due_iso") or "9999-12-31"),
        )[:limit]

    def _email_followup_in_view(
        self,
        due_date: date,
        today: date,
        view: str,
        include_upcoming: bool,
    ) -> bool:
        if include_upcoming or view == "all":
            return True
        if view == "today":
            return due_date == today
        if view == "overdue":
            return due_date < today
        if view == "upcoming":
            return due_date > today
        return due_date <= today

    def get_stats(self, today: date) -> dict:
        leads = self.get_all_leads()
        outreach_today = self.get_outreach_followups(today, view="today")
        outreach_overdue = self.get_outreach_followups(today, view="overdue")
        outreach_due = [*outreach_today, *outreach_overdue]
        proposal_followups_today = self.get_proposal_followups(today, view="today")
        proposal_followups_overdue = self.get_proposal_followups(today, view="overdue")
        proposal_followups_due = [*proposal_followups_today, *proposal_followups_overdue]
        open_proposals = self.get_proposals(today, view="open")
        stale_proposals = self.get_proposals(today, view="stale")
        stats = {
            "total": len(leads),
            "calls_today": len(self.get_call_leads("today", today)),
            "calls_overdue": len(self.get_call_leads("overdue", today)),
            "email_followups_today": len(outreach_today),
            "email_followups_overdue": len(outreach_overdue),
            "email_followups_due": len(outreach_due),
            "outreach_followups_today": len(outreach_today),
            "outreach_followups_overdue": len(outreach_overdue),
            "outreach_followups_due": len(outreach_due),
            "proposal_followups_today": len(proposal_followups_today),
            "proposal_followups_overdue": len(proposal_followups_overdue),
            "proposal_followups_due": len(proposal_followups_due),
            "open_proposals": len(open_proposals),
            "stale_proposals": len(stale_proposals),
            "impacted_today": len(self.get_impacted_leads(today)),
            "by_stage": {},
            "by_priority": {},
            "email_tasks_by_type": {},
            "outreach_tasks_by_type": {},
            "proposal_tasks_by_type": {},
            "proposal_status": {},
            "proposal_age_buckets": {"0-2": 0, "3-7": 0, "8-14": 0, "15+": 0},
        }

        for lead in leads:
            stage = (lead.get("stage") or "Blank").strip() or "Blank"
            priority = (lead.get("priority") or "Blank").strip() or "Blank"
            stats["by_stage"][stage] = stats["by_stage"].get(stage, 0) + 1
            stats["by_priority"][priority] = stats["by_priority"].get(priority, 0) + 1
            if self._has_actual_proposal(lead):
                status = self._proposal_status(lead) or "Sent"
                stats["proposal_status"][status] = stats["proposal_status"].get(status, 0) + 1

        for task in outreach_due:
            task_type = task["task_type"]
            stats["email_tasks_by_type"][task_type] = stats["email_tasks_by_type"].get(task_type, 0) + 1
            stats["outreach_tasks_by_type"][task_type] = stats["outreach_tasks_by_type"].get(task_type, 0) + 1

        for task in proposal_followups_due:
            task_type = task["task_type"]
            stats["proposal_tasks_by_type"][task_type] = stats["proposal_tasks_by_type"].get(task_type, 0) + 1

        for proposal in open_proposals:
            age = proposal.get("proposal_age_days") or 0
            if age <= 2:
                bucket = "0-2"
            elif age <= 7:
                bucket = "3-7"
            elif age <= 14:
                bucket = "8-14"
            else:
                bucket = "15+"
            stats["proposal_age_buckets"][bucket] += 1

        return stats

    def get_activity_history(self, today: date, days: int = 30) -> dict:
        days = max(1, min(int(days or 30), 120))
        start = today - timedelta(days=days - 1)
        daily = {
            (start + timedelta(days=offset)).isoformat(): {
                "date": (start + timedelta(days=offset)).isoformat(),
                "leads_impacted": 0,
                "new_leads_impacted": 0,
                "calls": 0,
                "emails_sent": 0,
                "_leads": set(),
                "_new_leads": set(),
            }
            for offset in range(days)
        }

        activity_rows = self._activity_rows()
        first_activity_date = self._first_activity_date(activity_rows)
        self._add_legacy_history(daily, start, today, first_activity_date)
        self._add_activity_log_history(daily, activity_rows, start, today)

        rows = []
        for day_key in sorted(daily):
            row = daily[day_key]
            row["leads_impacted"] = len(row.pop("_leads"))
            row["new_leads_impacted"] = len(row.pop("_new_leads"))
            rows.append(row)

        totals = {
            "leads_impacted": sum(row["leads_impacted"] for row in rows),
            "new_leads_impacted": sum(row["new_leads_impacted"] for row in rows),
            "calls": sum(row["calls"] for row in rows),
            "emails_sent": sum(row["emails_sent"] for row in rows),
        }
        return {"days": rows, "totals": totals}

    def update_lead(
        self,
        lead_id: str = "",
        updates: dict | None = None,
        row_number: int | str = "",
        mark_touched: bool = True,
        touched_date: date | None = None,
        activity: dict | None = None,
    ) -> bool:
        self.last_warning = ""
        updates = updates or {}
        match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
        if not match:
            logger.warning("PT Logistics lead not found", lead_id=lead_id)
            return False

        if mark_touched:
            updates = {**updates, "dashboard_touched": touched_date or date.today()}

        if "due" in updates and not updates.get("due"):
            updates.setdefault("due_time", "")
        if "due_time" in updates:
            updates["due_time"] = normalize_time(updates.get("due_time", ""))

        row_num, row = match
        cache_idx = row_num - 2
        before = self._row_to_dict(list(row), row_num)
        effective_date = touched_date or date.today()

        stage_update = (updates.get("stage") or "").strip().lower()
        if stage_update == "proposal sent":
            if not self._has_actual_proposal(before):
                updates.setdefault("proposal_sent", effective_date)
                updates.setdefault("proposal_status", "Sent")
            updates["due"] = ""
            updates["due_time"] = ""
        elif stage_update in {"lost", "not a fit", "meeting booked"} and self._has_actual_proposal(before):
            stage_status = {
                "lost": "Lost",
                "not a fit": "Not a Fit",
                "meeting booked": "Meeting Booked",
            }[stage_update]
            updates.setdefault("proposal_status", stage_status)

        cells = []

        for field, value in updates.items():
            col_idx = self._columns.get(field)
            if col_idx is None:
                continue
            formatted = format_sheet_date(value) if field in DATE_FIELDS else str(value)
            cells.append(Cell(row=row_num, col=col_idx + 1, value=formatted))
            while len(self._cache[cache_idx]) <= col_idx:
                self._cache[cache_idx].append("")
            self._cache[cache_idx][col_idx] = formatted

        if cells:
            self._api_call(self.sheet.update_cells, cells)

        callback_fields = {
            "due",
            "due_time",
            "calendar_event_id",
            "contact",
            "phone",
            "email",
            "stage",
            "notes",
            "what_happened",
        }
        if callback_fields.intersection(updates):
            after_for_calendar = self._row_to_dict(list(self._cache[cache_idx]), row_num)
            if (
                after_for_calendar.get("calendar_event_id")
                or after_for_calendar.get("due_iso")
                or before.get("calendar_event_id")
            ):
                self._sync_callback_calendar(row_num, before, after_for_calendar)

        if activity:
            after = self._row_to_dict(list(self._cache[cache_idx]), row_num)
            try:
                self._append_activity(
                    before=before,
                    after=after,
                    activity=activity,
                    touched_date=effective_date,
                )
            except Exception as exc:
                logger.warning(
                    "PT Logistics activity log append failed",
                    lead_id=lead_id,
                    row_number=row_number,
                    error=str(exc),
                )

        logger.info("PT Logistics lead updated", lead_id=lead_id, updates=list(updates.keys()))
        return True

    def append_note(self, lead_id: str, note: str, row_number: int | str = "") -> bool:
        if not note:
            return True

        match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
        if not match:
            return False

        _, row = match
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        existing = self._value(row, "notes")
        entry = f"[{timestamp}] {note}"
        notes = f"{entry}\n---\n{existing}" if existing.strip() else entry
        return self.update_lead(lead_id=lead_id, row_number=row_number, updates={"notes": notes})

    def log_call(
        self,
        lead_id: str,
        call_status: str,
        what_happened: str = "",
        notes: str = "",
        due: str = "",
        due_time: str = "",
        clear_due: bool = False,
        stage: str = "",
        row_number: int | str = "",
        touched_date: date | None = None,
    ) -> bool:
        call_status_norm = call_status.lower()
        updates = {
            "last_touch_type": call_status,
            "what_happened": what_happened or call_status,
        }
        if call_status_norm == "proposal sent":
            updates["stage"] = "Proposal Sent"
            updates["proposal_sent"] = touched_date or date.today()
            updates["proposal_status"] = "Sent"
            updates["due"] = ""
            updates["due_time"] = ""
        elif clear_due:
            updates["due"] = ""
            updates["due_time"] = ""
        elif due:
            updates["due"] = due
            updates["due_time"] = due_time
        if stage:
            updates["stage"] = stage
        elif call_status_norm == "send email":
            updates["stage"] = "Send Email"
            updates["due"] = ""
            updates["due_time"] = ""
        elif call_status_norm == "email sent":
            updates["stage"] = "Email Sent"
        elif call_status_norm == "no answer":
            updates["stage"] = "No Answer"
        elif call_status:
            updates["stage"] = "Call Back" if due else "Contacted"

        if notes:
            match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
            if not match:
                return False
            _, row = match
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            existing = self._value(row, "notes")
            entry = f"[{timestamp}] {notes}"
            updates["notes"] = f"{entry}\n---\n{existing}" if existing.strip() else entry

        email_task = ""
        if call_status_norm == "proposal sent":
            email_task = "Proposal"
        elif call_status_norm == "email sent":
            email_task = "Manual"

        return self.update_lead(
            lead_id=lead_id,
            row_number=row_number,
            updates=updates,
            touched_date=touched_date,
            activity={
                "event_type": "call",
                "call_status": call_status,
                "email_task": email_task,
                "notes": notes or what_happened or call_status,
            },
        )

    def mark_manual_email_sent(
        self,
        lead_id: str,
        sent_date: date,
        notes: str = "",
        row_number: int | str = "",
        touched_date: date | None = None,
    ) -> bool:
        match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
        if not match:
            return False

        row_num, row = match
        lead = self._row_to_dict(list(row), row_num)
        rule = self._next_outreach_rule(lead)
        task_type = rule["type"] if rule else "Initial"
        updates = {
            "last_touch_type": "Email sent",
            "what_happened": "Email sent",
            "stage": "Email Sent",
            "due": "",
        }
        if rule:
            updates[rule["target_field"]] = sent_date

        if notes:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            existing = self._value(row, "notes")
            entry = f"[{timestamp}] Email sent. {notes}"
            updates["notes"] = f"{entry}\n---\n{existing}" if existing.strip() else entry

        return self.update_lead(
            lead_id=lead_id,
            row_number=row_number,
            updates=updates,
            touched_date=touched_date,
            activity={
                "event_type": "email",
                "email_task": task_type or "Initial",
                "notes": notes or "Email sent",
            },
        )

    def _next_outreach_rule(self, lead: dict) -> dict | None:
        for rule in OUTREACH_FOLLOWUP_RULES:
            if not self._rule_target_filled(lead, rule):
                return rule
        return None

    def _proposal_rule(self, task_type: str) -> dict | None:
        return next((r for r in PROPOSAL_FOLLOWUP_RULES if r["type"].lower() == task_type.lower()), None)

    def _outreach_rule(self, task_type: str) -> dict | None:
        return next((r for r in OUTREACH_FOLLOWUP_RULES if r["type"].lower() == task_type.lower()), None)

    def mark_email_followup_sent(
        self,
        lead_id: str,
        task_type: str,
        sent_date: date,
        notes: str = "",
        row_number: int | str = "",
        touched_date: date | None = None,
    ) -> bool:
        if task_type.lower().startswith("proposal"):
            return self.mark_proposal_followup_sent(
                lead_id=lead_id,
                task_type=task_type,
                sent_date=sent_date,
                notes=notes,
                row_number=row_number,
                touched_date=touched_date,
            )

        return self.mark_outreach_followup_sent(
            lead_id=lead_id,
            task_type=task_type,
            sent_date=sent_date,
            notes=notes,
            row_number=row_number,
            touched_date=touched_date,
        )

    def mark_outreach_followup_sent(
        self,
        lead_id: str,
        task_type: str,
        sent_date: date,
        notes: str = "",
        row_number: int | str = "",
        touched_date: date | None = None,
    ) -> bool:
        rule = self._outreach_rule(task_type)
        if not rule:
            return False

        updates = {
            rule["target_field"]: sent_date,
            "last_touch_type": "Email sent",
            "what_happened": rule["label"],
            "stage": "Email Sent",
            "due": "",
        }

        if notes:
            match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
            if not match:
                return False
            _, row = match
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            existing = self._value(row, "notes")
            entry = f"[{timestamp}] {rule['type']} sent. {notes}"
            updates["notes"] = f"{entry}\n---\n{existing}" if existing.strip() else entry

        return self.update_lead(
            lead_id=lead_id,
            row_number=row_number,
            updates=updates,
            touched_date=touched_date,
            activity={
                "event_type": "email",
                "email_task": rule["type"],
                "notes": notes or rule["label"],
            },
        )

    def mark_proposal_followup_sent(
        self,
        lead_id: str,
        task_type: str,
        sent_date: date,
        notes: str = "",
        row_number: int | str = "",
        touched_date: date | None = None,
    ) -> bool:
        if task_type.lower() == "proposal next action":
            updates = {
                "proposal_next_action": "",
                "proposal_next_action_due": "",
                "last_touch_type": "Proposal next action",
                "what_happened": "Proposal next action completed",
            }
            if notes:
                match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
                if not match:
                    return False
                _, row = match
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                existing = self._value(row, "notes")
                entry = f"[{timestamp}] Proposal next action completed. {notes}"
                updates["notes"] = f"{entry}\n---\n{existing}" if existing.strip() else entry

            return self.update_lead(
                lead_id=lead_id,
                row_number=row_number,
                updates=updates,
                touched_date=touched_date,
                activity={
                    "event_type": "proposal_update",
                    "email_task": "",
                    "notes": notes or "Proposal next action completed",
                },
            )

        if task_type.lower() == "send proposal":
            updates = {
                "proposal_sent": sent_date,
                "proposal_status": "Sent",
                "stage": "Proposal Sent",
                "last_touch_type": "Proposal sent",
                "what_happened": "Proposal sent",
                "due": "",
            }
            if notes:
                match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
                if not match:
                    return False
                _, row = match
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                existing = self._value(row, "notes")
                entry = f"[{timestamp}] Proposal sent. {notes}"
                updates["notes"] = f"{entry}\n---\n{existing}" if existing.strip() else entry

            return self.update_lead(
                lead_id=lead_id,
                row_number=row_number,
                updates=updates,
                touched_date=touched_date,
                activity={
                    "event_type": "proposal_email",
                    "email_task": "Send Proposal",
                    "notes": notes or "Proposal sent",
                },
            )

        rule = self._proposal_rule(task_type)
        if not rule:
            return False

        updates = {
            rule["target_field"]: sent_date,
            "last_touch_type": "Proposal follow-up",
            "what_happened": rule["label"],
            "proposal_status": rule["next_status"],
            "proposal_next_action": "",
            "proposal_next_action_due": "",
        }

        if notes:
            match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
            if not match:
                return False
            _, row = match
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            existing = self._value(row, "notes")
            entry = f"[{timestamp}] {rule['type']} sent. {notes}"
            updates["notes"] = f"{entry}\n---\n{existing}" if existing.strip() else entry

        return self.update_lead(
            lead_id=lead_id,
            row_number=row_number,
            updates=updates,
            touched_date=touched_date,
            activity={
                "event_type": "proposal_email",
                "email_task": rule["type"],
                "notes": notes or rule["label"],
            },
        )

    def update_proposal(
        self,
        lead_id: str,
        row_number: int | str = "",
        status: str = "",
        next_action: str | None = None,
        next_action_due: str | None = None,
        outcome: str = "",
        lost_reason: str = "",
        value: str = "",
        probability: str = "",
        forecast_category: str = "",
        notes: str = "",
        touched_date: date | None = None,
    ) -> bool:
        updates = {}
        if status:
            updates["proposal_status"] = status
            status_lower = status.strip().lower()
            if status_lower in {"lost", "not a fit"}:
                updates["stage"] = "Lost" if status_lower == "lost" else "Not a Fit"
            elif status_lower in {"meeting booked", "won"}:
                updates["stage"] = "Meeting Booked"
        if next_action is not None:
            updates["proposal_next_action"] = next_action
        if next_action_due is not None:
            updates["proposal_next_action_due"] = next_action_due
        if outcome:
            updates["proposal_outcome"] = outcome
        if lost_reason:
            updates["proposal_lost_reason"] = lost_reason
        if value:
            updates["proposal_value"] = value
        if probability:
            updates["proposal_probability"] = probability
        if forecast_category:
            updates["forecast_category"] = forecast_category

        if notes:
            match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
            if not match:
                return False
            _, row = match
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            existing = self._value(row, "notes")
            entry = f"[{timestamp}] Proposal update. {notes}"
            updates["notes"] = f"{entry}\n---\n{existing}" if existing.strip() else entry

        return self.update_lead(
            lead_id=lead_id,
            row_number=row_number,
            updates=updates,
            touched_date=touched_date,
            activity={
                "event_type": "proposal_update",
                "notes": notes or status or next_action or "Proposal update",
            },
        )

    def _activity_rows(self) -> list[dict]:
        values = self._api_call(self.activity_sheet.get_all_values)
        if len(values) <= 1:
            return []
        headers = values[0]
        rows = []
        for raw in values[1:]:
            row = {header: raw[idx] if idx < len(raw) else "" for idx, header in enumerate(headers)}
            rows.append(row)
        return rows

    def _first_activity_date(self, activity_rows: list[dict]) -> Optional[date]:
        dates = [parse_sheet_date(row.get("Date", "")) for row in activity_rows]
        dates = [value for value in dates if value]
        return min(dates) if dates else None

    def _counts_as_completed_call(self, call_status: str) -> bool:
        return (call_status or "").strip().lower() not in {"", "no answer"}

    def _add_legacy_history(
        self,
        daily: dict,
        start: date,
        today: date,
        first_activity_date: Optional[date],
    ):
        email_fields = (
            "initial_email_sent",
            "outreach_fu1_sent",
            "outreach_fu2_sent",
            "outreach_fu3_sent",
            "outreach_reactivation_sent",
            "proposal_sent",
            "proposal_fu1_sent",
            "proposal_fu2_sent",
            "proposal_fu3_sent",
            "proposal_reactivation_sent",
            "fu1_sent",
            "fu2_sent",
            "fu3_sent",
            "reactivation_sent",
        )
        for i, row in enumerate(self._cache):
            lead = self._row_to_dict(list(row), i + 2)
            key = lead.get("key") or f"row-{i + 2}"
            touched = parse_sheet_date(lead.get("dashboard_touched", ""))
            if touched and start <= touched <= today and (not first_activity_date or touched < first_activity_date):
                day = daily[touched.isoformat()]
                day["_leads"].add(key)
                last_touch = (lead.get("last_touch_type") or "").strip().lower()
                if last_touch != "email sent" and self._counts_as_completed_call(last_touch):
                    day["calls"] += 1

            for field in email_fields:
                if field.startswith("proposal_") and not self._has_actual_proposal(lead):
                    continue
                sent = parse_sheet_date(lead.get(field, ""))
                if sent and start <= sent <= today and (not first_activity_date or sent < first_activity_date):
                    daily[sent.isoformat()]["emails_sent"] += 1

    def _add_activity_log_history(
        self,
        daily: dict,
        activity_rows: list[dict],
        start: date,
        today: date,
    ):
        for row in activity_rows:
            event_date = parse_sheet_date(row.get("Date", ""))
            if not event_date or event_date < start or event_date > today:
                continue
            day = daily[event_date.isoformat()]
            key = row.get("Lead Key") or row.get("Row Number") or row.get("Company")
            if row.get("Full Lead Impacted", "").lower() == "yes" and key:
                day["_leads"].add(key)
            if row.get("New Lead Impacted", "").lower() == "yes" and key:
                day["_new_leads"].add(key)
            event_type = (row.get("Event Type") or "").strip().lower()
            if event_type == "call" and self._counts_as_completed_call(row.get("Call Status", "")):
                day["calls"] += 1
            if event_type == "email" or row.get("Email Task"):
                day["emails_sent"] += 1

    def _append_activity(self, before: dict, after: dict, activity: dict, touched_date: date):
        event_type = activity.get("event_type", "update")
        call_status = activity.get("call_status", "")
        email_task = activity.get("email_task", "")
        new_lead_impacted = self._is_new_lead_activity(
            stage_before=before.get("stage", ""),
            stage_after=after.get("stage", ""),
            event_type=event_type,
            call_status=call_status,
        )
        full_lead_impacted = event_type in {"call", "email", "update"}

        self._api_call(
            self.activity_sheet.append_row,
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                touched_date.isoformat(),
                event_type,
                after.get("key") or before.get("key") or "",
                after.get("row_number") or before.get("row_number") or "",
                after.get("company") or before.get("company") or "",
                before.get("stage", ""),
                after.get("stage", ""),
                before.get("due", ""),
                after.get("due", ""),
                call_status,
                email_task,
                "yes" if new_lead_impacted else "no",
                "yes" if full_lead_impacted else "no",
                activity.get("notes", ""),
                before.get("proposal_status_effective", ""),
                after.get("proposal_status_effective", ""),
            ],
            table_range="A1",
        )

        proposal_before = before.get("proposal_status_effective", "")
        proposal_after = after.get("proposal_status_effective", "")
        if (
            before.get("stage", "") != after.get("stage", "")
            or proposal_before != proposal_after
            or event_type.startswith("proposal")
        ):
            self._append_stage_event(
                before=before,
                after=after,
                activity=activity,
                touched_date=touched_date,
            )

    def _append_stage_event(self, before: dict, after: dict, activity: dict, touched_date: date):
        self._api_call(
            self.stage_event_sheet.append_row,
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                touched_date.isoformat(),
                after.get("key") or before.get("key") or "",
                after.get("row_number") or before.get("row_number") or "",
                after.get("company") or before.get("company") or "",
                activity.get("event_type", "update"),
                before.get("stage", ""),
                after.get("stage", ""),
                before.get("proposal_status_effective", ""),
                after.get("proposal_status_effective", ""),
                activity.get("call_status", ""),
                activity.get("email_task", ""),
                activity.get("notes", ""),
            ],
            table_range="A1",
        )

    def _stage_event_rows(self) -> list[dict]:
        try:
            values = self._api_call(self.stage_event_sheet.get_all_values)
        except Exception:
            return []
        if len(values) <= 1:
            return []
        headers = values[0]
        rows = []
        for raw in values[1:]:
            rows.append({header: raw[idx] if idx < len(raw) else "" for idx, header in enumerate(headers)})
        return rows

    def get_stage_timing(self, today: date, days: int = 120) -> dict:
        start = today - timedelta(days=max(1, min(int(days or 120), 365)) - 1)
        rows = []
        stage_rows = self._stage_event_rows()
        rows.extend(stage_rows)
        stage_dates = [parse_sheet_date(row.get("Date", "")) for row in stage_rows]
        stage_dates = [value for value in stage_dates if value]
        first_stage_event_date = min(stage_dates) if stage_dates else None

        # Existing Dashboard Activity rows provide useful stage timing for the
        # period before Lead Stage Events existed.
        for row in self._activity_rows():
            event_date = parse_sheet_date(row.get("Date", ""))
            if first_stage_event_date and event_date and event_date >= first_stage_event_date:
                continue
            rows.append({
                "Timestamp": row.get("Timestamp", ""),
                "Date": row.get("Date", ""),
                "Lead Key": row.get("Lead Key", ""),
                "Row Number": row.get("Row Number", ""),
                "Company": row.get("Company", ""),
                "Event Type": row.get("Event Type", ""),
                "Stage Before": row.get("Stage Before", ""),
                "Stage After": row.get("Stage After", ""),
                "Proposal Status Before": row.get("Proposal Status Before", ""),
                "Proposal Status After": row.get("Proposal Status After", ""),
                "Call Status": row.get("Call Status", ""),
                "Email Task": row.get("Email Task", ""),
                "Notes": row.get("Notes", ""),
            })

        lead_events: dict[str, list[dict]] = {}
        for row in rows:
            event_date = parse_sheet_date(row.get("Date", ""))
            if not event_date or event_date < start or event_date > today:
                continue
            key = row.get("Lead Key") or row.get("Row Number") or row.get("Company")
            if not key:
                continue
            lead_events.setdefault(key, []).append({**row, "_date": event_date})

        transition_days: dict[str, list[int]] = {}
        transition_counts: dict[str, int] = {}
        proposal_transition_counts: dict[str, int] = {}
        for events in lead_events.values():
            events.sort(key=lambda row: (row["_date"], row.get("Timestamp", "")))
            last_stage = ""
            last_stage_date: date | None = None
            last_proposal = ""
            last_proposal_date: date | None = None

            for row in events:
                event_date = row["_date"]
                stage_before = (row.get("Stage Before") or "").strip() or "Blank"
                stage_after = (row.get("Stage After") or "").strip() or "Blank"
                proposal_before = (row.get("Proposal Status Before") or "").strip() or "Blank"
                proposal_after = (row.get("Proposal Status After") or "").strip() or "Blank"

                if stage_before != stage_after:
                    key = f"{stage_before} -> {stage_after}"
                    transition_counts[key] = transition_counts.get(key, 0) + 1
                    if last_stage_date and last_stage == stage_before:
                        transition_days.setdefault(key, []).append((event_date - last_stage_date).days)
                    last_stage = stage_after
                    last_stage_date = event_date

                if proposal_before != proposal_after:
                    key = f"{proposal_before} -> {proposal_after}"
                    proposal_transition_counts[key] = proposal_transition_counts.get(key, 0) + 1
                    if last_proposal_date and last_proposal == proposal_before:
                        transition_days.setdefault(f"Proposal: {key}", []).append((event_date - last_proposal_date).days)
                    last_proposal = proposal_after
                    last_proposal_date = event_date

        def summarize(values: list[int]) -> dict:
            values = sorted(values)
            return {
                "n": len(values),
                "avg_days": round(sum(values) / len(values), 2),
                "median_days": values[len(values) // 2],
                "min_days": values[0],
                "max_days": values[-1],
            }

        return {
            "stage_transitions": dict(sorted(transition_counts.items(), key=lambda item: (-item[1], item[0]))),
            "proposal_transitions": dict(sorted(proposal_transition_counts.items(), key=lambda item: (-item[1], item[0]))),
            "transition_timing": {
                key: summarize(values)
                for key, values in sorted(transition_days.items())
                if values
            },
        }

    def _is_new_lead_activity(
        self,
        stage_before: str,
        stage_after: str,
        event_type: str,
        call_status: str = "",
    ) -> bool:
        call_status_norm = (call_status or "").strip().lower()
        if call_status_norm == "no answer":
            return False

        before = (stage_before or "").strip().lower()
        after = (stage_after or "").strip().lower()
        had_no_real_data = before in {"", "new", "no answer"}
        has_real_result = after not in {"", "new", "no answer"}
        return event_type in {"call", "email"} and had_no_real_data and has_real_result
