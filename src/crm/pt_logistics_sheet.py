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
from google.oauth2.service_account import Credentials
from gspread.cell import Cell
from gspread.exceptions import APIError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = structlog.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_API_CALL_INTERVAL = 1.5
ACTIVITY_LOG_SHEET_NAME = "Dashboard Activity"

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
    "Proposal Sent",
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
    "proposal_sent": "Proposal Sent",
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

TERMINAL_STAGES = {"lost", "not a fit"}
PROPOSAL_STAGES = {"send proposal", "proposal requested", "proposal to send"}
EMAIL_TASK_STAGES = {"send email", "email sent"}

FOLLOWUP_RULES = [
    {
        "type": "Proposal",
        "source_field": "",
        "target_field": "proposal_sent",
        "days_after": 0,
        "label": "Initial proposal email",
    },
    {
        "type": "FU1",
        "source_field": "proposal_sent",
        "target_field": "fu1_sent",
        "days_after": 2,
        "label": "First follow-up",
    },
    {
        "type": "FU2",
        "source_field": "fu1_sent",
        "target_field": "fu2_sent",
        "days_after": 3,
        "label": "Second follow-up",
    },
    {
        "type": "FU3",
        "source_field": "fu2_sent",
        "target_field": "fu3_sent",
        "days_after": 5,
        "label": "Third follow-up",
    },
    {
        "type": "Reactivation",
        "source_field": "fu3_sent",
        "target_field": "reactivation_sent",
        "days_after": 30,
        "label": "Reactivation email",
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


def is_filled(value: str) -> bool:
    """Treat dates and X/x markers as completed follow-up fields."""
    return bool((value or "").strip())


class PTLogisticsCRM:
    """Google Sheets CRM wrapper for the active PT Logistics workflow."""

    def __init__(self, credentials_file: str, spreadsheet_id: str, sheet_name: str = "PT Logistics"):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._last_api_call = 0.0
        self._cache: list[list[str]] = []
        self.headers: list[str] = []
        self._columns: dict[str, int] = {}

        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.spreadsheet = self._api_call(self.client.open_by_key, spreadsheet_id)
        self.sheet = self._get_or_create_sheet(sheet_name)
        self._ensure_headers()
        self.activity_sheet = self._get_or_create_activity_sheet()
        self._ensure_activity_headers()
        self._refresh_cache()

        logger.info("PTLogisticsCRM initialized", spreadsheet_id=spreadsheet_id, sheet=sheet_name)

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
        if current_headers != ACTIVITY_LOG_HEADERS:
            self._api_call(self.activity_sheet.update, "A1", [ACTIVITY_LOG_HEADERS])

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

    def _row_to_dict(self, row: list[str], row_number: int | None = None) -> dict:
        result = {}
        for field in FIELD_ALIASES:
            result[field] = self._value(row, field)
        result["row_number"] = row_number or ""
        result["key"] = result.get("id") or f"row-{row_number}" if row_number else result.get("id", "")

        due_date = parse_sheet_date(result.get("due", ""))
        result["due_iso"] = due_date.isoformat() if due_date else ""

        for field in ("proposal_sent", "fu1_sent", "fu2_sent", "fu3_sent", "reactivation_sent"):
            parsed = parse_sheet_date(result.get(field, ""))
            result[f"{field}_iso"] = parsed.isoformat() if parsed else ""

        touched_date = parse_sheet_date(result.get("dashboard_touched", ""))
        result["dashboard_touched_iso"] = touched_date.isoformat() if touched_date else ""

        return result

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

        return sorted(leads, key=lambda lead: lead.get("due_iso") or "9999-12-31")

    def get_email_followups(
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

            for rule in FOLLOWUP_RULES:
                if rule["type"] == "Proposal":
                    if is_filled(lead.get("proposal_sent", "")):
                        continue
                    if stage in PROPOSAL_STAGES:
                        due_date = today
                        if self._email_followup_in_view(due_date, today, view, include_upcoming):
                            tasks.append({
                                **lead,
                                "task_type": rule["type"],
                                "task_label": rule["label"],
                                "task_due": "Now",
                                "task_due_iso": due_date.isoformat(),
                                "task_overdue_days": 0,
                                "source_field": rule["source_field"],
                                "target_field": rule["target_field"],
                                "based_on": "Call outcome",
                            })
                        break
                    continue

                if rule["type"] == "FU1" and stage in EMAIL_TASK_STAGES and not is_filled(lead.get("fu1_sent", "")):
                    due_date = (
                        parse_sheet_date(lead.get("due", ""))
                        or parse_sheet_date(lead.get("dashboard_touched", ""))
                        or (today if stage == "send email" else None)
                    )
                    if due_date and self._email_followup_in_view(due_date, today, view, include_upcoming):
                        tasks.append({
                            **lead,
                            "task_type": rule["type"],
                            "task_label": "Send email" if stage == "send email" else "Initial email after call",
                            "task_due": due_date.strftime("%Y/%m/%d"),
                            "task_due_iso": due_date.isoformat(),
                            "task_overdue_days": max((today - due_date).days, 0),
                            "source_field": "dashboard_touched",
                            "target_field": rule["target_field"],
                            "based_on": "Email stage",
                        })
                    break

                source_value = lead.get(rule["source_field"], "")
                target_value = lead.get(rule["target_field"], "")
                source_date = parse_sheet_date(source_value)

                if not source_date or is_filled(target_value):
                    continue

                due_date = source_date + timedelta(days=rule["days_after"])
                if self._email_followup_in_view(due_date, today, view, include_upcoming):
                    tasks.append({
                        **lead,
                        "task_type": rule["type"],
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
        followups_today = self.get_email_followups(today, view="today")
        followups_overdue = self.get_email_followups(today, view="overdue")
        followups = [*followups_today, *followups_overdue]
        stats = {
            "total": len(leads),
            "calls_today": len(self.get_call_leads("today", today)),
            "calls_overdue": len(self.get_call_leads("overdue", today)),
            "email_followups_today": len(followups_today),
            "email_followups_overdue": len(followups_overdue),
            "email_followups_due": len(followups),
            "impacted_today": len(self.get_impacted_leads(today)),
            "by_stage": {},
            "by_priority": {},
            "email_tasks_by_type": {},
        }

        for lead in leads:
            stage = (lead.get("stage") or "Blank").strip() or "Blank"
            priority = (lead.get("priority") or "Blank").strip() or "Blank"
            stats["by_stage"][stage] = stats["by_stage"].get(stage, 0) + 1
            stats["by_priority"][priority] = stats["by_priority"].get(priority, 0) + 1

        for task in followups:
            task_type = task["task_type"]
            stats["email_tasks_by_type"][task_type] = stats["email_tasks_by_type"].get(task_type, 0) + 1

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
        updates = updates or {}
        match = self._find_by_reference(lead_id=lead_id, row_number=row_number)
        if not match:
            logger.warning("PT Logistics lead not found", lead_id=lead_id)
            return False

        if mark_touched:
            updates = {**updates, "dashboard_touched": touched_date or date.today()}

        row_num, row = match
        cache_idx = row_num - 2
        before = self._row_to_dict(list(row), row_num)
        cells = []

        for field, value in updates.items():
            col_idx = self._columns.get(field)
            if col_idx is None:
                continue
            formatted = format_sheet_date(value) if field in {
                "due",
                "proposal_sent",
                "fu1_sent",
                "fu2_sent",
                "fu3_sent",
                "reactivation_sent",
                "meeting_date",
                "dashboard_touched",
            } else str(value)
            cells.append(Cell(row=row_num, col=col_idx + 1, value=formatted))
            while len(self._cache[cache_idx]) <= col_idx:
                self._cache[cache_idx].append("")
            self._cache[cache_idx][col_idx] = formatted

        if cells:
            self._api_call(self.sheet.update_cells, cells)

        if activity:
            after = self._row_to_dict(list(self._cache[cache_idx]), row_num)
            try:
                self._append_activity(
                    before=before,
                    after=after,
                    activity=activity,
                    touched_date=touched_date or date.today(),
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
            updates["due"] = ""
        elif clear_due:
            updates["due"] = ""
        elif due:
            updates["due"] = due
        if stage:
            updates["stage"] = stage
        elif call_status_norm == "send email":
            updates["stage"] = "Send Email"
            updates["due"] = ""
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
        task_type = self._next_email_task_type(lead)
        rule = next((r for r in FOLLOWUP_RULES if r["type"] == task_type), None)
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
                "email_task": task_type or "Manual",
                "notes": notes or "Email sent",
            },
        )

    def _next_email_task_type(self, lead: dict) -> str:
        for rule in FOLLOWUP_RULES:
            if rule["type"] == "Proposal":
                continue
            if not is_filled(lead.get(rule["target_field"], "")):
                return rule["type"]
        return ""

    def mark_email_followup_sent(
        self,
        lead_id: str,
        task_type: str,
        sent_date: date,
        notes: str = "",
        row_number: int | str = "",
        touched_date: date | None = None,
    ) -> bool:
        rule = next((r for r in FOLLOWUP_RULES if r["type"].lower() == task_type.lower()), None)
        if not rule:
            return False

        next_stage = "Email Sent"
        if rule["type"] == "Proposal":
            next_stage = "Proposal Sent"

        updates = {
            rule["target_field"]: sent_date,
            "last_touch_type": "Email sent",
            "what_happened": rule["label"],
            "stage": next_stage,
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

    def _add_legacy_history(
        self,
        daily: dict,
        start: date,
        today: date,
        first_activity_date: Optional[date],
    ):
        email_fields = ("proposal_sent", "fu1_sent", "fu2_sent", "fu3_sent", "reactivation_sent")
        for i, row in enumerate(self._cache):
            lead = self._row_to_dict(list(row), i + 2)
            key = lead.get("key") or f"row-{i + 2}"
            touched = parse_sheet_date(lead.get("dashboard_touched", ""))
            if touched and start <= touched <= today and (not first_activity_date or touched < first_activity_date):
                day = daily[touched.isoformat()]
                day["_leads"].add(key)
                last_touch = (lead.get("last_touch_type") or "").strip().lower()
                if last_touch and last_touch != "email sent":
                    day["calls"] += 1

            for field in email_fields:
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
            if event_type == "call":
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
            ],
            table_range="A1",
        )

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
