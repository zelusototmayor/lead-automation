"""Google Calendar callback event sync for scheduled CRM calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

import structlog
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

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
    """Create, update, and delete 10-minute callback events."""

    def __init__(
        self,
        credentials_file: str,
        calendar_id: str = "",
        timezone: str = "Europe/Lisbon",
        duration_minutes: int = 10,
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
                    if self._is_callback_event(existing.json()):
                        response = self._request("PATCH", event_path, json=payload)
                        response.raise_for_status()
                        return CalendarSyncResult(ok=True, event_id=response.json().get("id", event_id))
                    event_id = ""

            response = self._request(
                "POST",
                f"/calendars/{self._quote(self.calendar_id)}/events",
                json=payload,
            )
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
        event_id = (event_id or "").strip()
        if not event_id:
            return CalendarSyncResult(ok=True, event_id="")
        if not self.configured():
            return CalendarSyncResult(
                ok=False,
                event_id=event_id,
                warning="Lead saved, but the existing calendar event was not deleted because CALLBACK_CALENDAR_ID is not configured.",
            )

        try:
            event_path = f"/calendars/{self._quote(self.calendar_id)}/events/{self._quote(event_id)}"
            existing = self._request("GET", event_path)
            if existing.status_code in (404, 410):
                return CalendarSyncResult(ok=True, event_id="")
            existing.raise_for_status()
            if not self._is_callback_event(existing.json()):
                return CalendarSyncResult(
                    ok=True,
                    event_id="",
                    warning=(
                        "Existing calendar event was not deleted because it is not a PT Logistics callback; "
                        "the stale calendar_event_id was cleared."
                    ),
                )
            response = self._request("DELETE", event_path)
            if response.status_code not in (200, 204, 404, 410):
                response.raise_for_status()
            return CalendarSyncResult(ok=True, event_id="")
        except Exception as exc:
            logger.warning("Calendar callback event delete failed", error=str(exc))
            return CalendarSyncResult(
                ok=False,
                event_id=event_id,
                warning=f"Lead saved, but calendar event could not be deleted: {exc}",
            )

    def _request(self, method: str, path: str, **kwargs):
        if self._session is None:
            creds = Credentials.from_service_account_file(
                self._credentials_file,
                scopes=[CALENDAR_SCOPE],
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
