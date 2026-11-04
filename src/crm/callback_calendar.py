"""Google Calendar callback event sync for scheduled CRM calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from urllib.parse import quote
from zoneinfo import ZoneInfo

import structlog
from google.auth.transport.requests import AuthorizedSession

from src.crm.google_credentials import load_google_credentials

logger = structlog.get_logger()

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
CALLBACK_PRIVATE_PROPERTY = "pt_logistics_callback"


@dataclass
class CalendarSyncResult:
    ok: bool
    event_id: str = ""
    warning: str = ""


class CallbackCalendar:
    """Append 15-minute callbacks without deleting earlier Calendar history."""

    def __init__(
        self,
        credentials_file: str,
        calendar_id: str = "",
        timezone: str = "Europe/Lisbon",
        duration_minutes: int = 15,
    ):
        self.calendar_id = (calendar_id or "").strip()
        self.timezone = timezone
        self.duration_minutes = duration_minutes
        self._session: AuthorizedSession | None = None
        self._credentials_file = credentials_file

    def configured(self) -> bool:
        return bool(self.calendar_id)

    def upsert_event(
        self,
        *,
        event_id: str = "",
        due_date: str,
        due_time: str,
        title: str,
        description: str,
        callback_key: str = "",
    ) -> CalendarSyncResult:
        if not self.configured():
            return CalendarSyncResult(
                ok=False,
                event_id=event_id,
                warning="Callback calendar is not configured. Set CALLBACK_CALENDAR_ID to create calendar events.",
            )

        start = self._event_datetime(due_date, due_time)
        if not start:
            return CalendarSyncResult(ok=True, event_id=event_id)
        end = start + timedelta(minutes=self.duration_minutes)
        payload = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat(timespec="seconds"), "timeZone": self.timezone},
            "end": {"dateTime": end.isoformat(timespec="seconds"), "timeZone": self.timezone},
            "extendedProperties": {
                "private": {CALLBACK_PRIVATE_PROPERTY: "1"},
            },
        }

        try:
            if event_id:
                event_path = f"/calendars/{self._quote(self.calendar_id)}/events/{self._quote(event_id)}"
                existing = self._request("GET", event_path)
                if existing.status_code in (404, 410):
                    event_id = ""
                else:
                    existing.raise_for_status()
                    if (self._is_callback_event(existing.json())
                            and self._same_callback(existing.json(), payload)):
                        return CalendarSyncResult(ok=True, event_id=event_id)
                    event_id = ""

            # Legacy Sheets callers have no canonical task ID. Include their
            # stable lead key and complete note/schedule snapshot in the event
            # ID so retries after an uncertain POST do not create duplicates.
            intent = {"calendar": self.calendar_id, "key": callback_key, "event": payload}
            event_id = "crm" + hashlib.sha256(json.dumps(
                intent, sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            response = self._request(
                "POST",
                f"/calendars/{self._quote(self.calendar_id)}/events",
                json=payload | {"id": event_id},
                params={"sendUpdates": "none"},
            )
            if response.status_code == 409:
                response = self._request("GET", f"/calendars/{self._quote(self.calendar_id)}/events/{event_id}")
                response.raise_for_status()
                if not self._is_callback_event(response.json()) or not self._same_callback(response.json(), payload):
                    raise RuntimeError("Calendar callback conflict; earlier event preserved")
            response.raise_for_status()
            return CalendarSyncResult(ok=True, event_id=response.json().get("id", ""))
        except Exception as exc:
            logger.warning("Calendar callback event sync failed", error=str(exc))
            return CalendarSyncResult(
                ok=False,
                event_id=event_id,
                warning=f"Lead saved, but calendar event could not be synced: {exc}",
            )

    def delete_event(self, event_id: str) -> CalendarSyncResult:
        """Retire the CRM pointer only; Calendar deletion is always manual."""
        return CalendarSyncResult(ok=True, event_id="")

    def _same_callback(self, event: dict, payload: dict) -> bool:
        if event.get("status") == "cancelled":
            return False
        if any(event.get(key) != payload[key] for key in ("summary", "description")):
            return False
        try:
            def local_date(value):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=ZoneInfo(self.timezone)) if parsed.tzinfo is None else parsed
            return all(
                local_date(event[key]["dateTime"]) == local_date(payload[key]["dateTime"])
                for key in ("start", "end")
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _request(self, method: str, path: str, **kwargs):
        if self._session is None:
            creds = load_google_credentials(
                self._credentials_file,
                [CALENDAR_SCOPE],
            )
            self._session = AuthorizedSession(creds)
        return self._session.request(method, f"{CALENDAR_API_BASE}{path}", **kwargs)

    def _event_datetime(self, due_date: str, due_time: str) -> datetime | None:
        due_date = (due_date or "").strip().replace("/", "-")
        due_time = (due_time or "").strip()
        if not due_date or not due_time:
            return None
        try:
            return datetime.strptime(f"{due_date} {due_time}", "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    @staticmethod
    def _quote(value: str) -> str:
        return quote(value, safe="")

    @staticmethod
    def _is_callback_event(event: dict) -> bool:
        private_properties = (
            (event.get("extendedProperties") or {}).get("private") or {}
        )
        return private_properties.get(CALLBACK_PRIVATE_PROPERTY) == "1"
