"""Deterministic, owner-checked Calendar projection of one independent call task."""

from __future__ import annotations

from datetime import timedelta
import os
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import select
from src.crm.callback_calendar import CallbackCalendar
from src.crm.persistence.models import Task, Lead, SourceIdentity
from src.crm.services.agent_work_service import locked_work, validate_lease, finish_work


class CalendarProjectionError(RuntimeError):
    pass


class CanonicalCallbackCalendar(CallbackCalendar):
    """A provider retry uses the same event ID, even after an uncertain POST."""

    def sync_task(self, task, *, legacy_event_id=""):
        if not self.configured():
            raise CalendarProjectionError("Calendar integration is not configured")
        canonical_id = "crm" + task.id.hex
        event_id = legacy_event_id or canonical_id
        collection = f"/calendars/{quote(self.calendar_id, safe='')}/events"
        path = f"{collection}/{quote(event_id, safe='')}"
        owner = {
            "crm_task_id": str(task.id),
            "crm_workspace_id": str(task.workspace_id),
        }
        existing = self._request("GET", path, timeout=20)
        if legacy_event_id and existing.status_code in {404, 410}:
            event_id = canonical_id
            path = f"{collection}/{event_id}"
            existing = self._request("GET", path, timeout=20)
        if existing.status_code not in {404, 410}:
            existing.raise_for_status()
            if legacy_event_id and event_id == legacy_event_id:
                private = (
                    existing.json().get("extendedProperties", {}).get("private", {})
                )
                if private.get("pt_logistics_callback") != "1" or any(
                    private.get(key) not in (None, value)
                    for key, value in owner.items()
                ):
                    raise CalendarProjectionError(
                        "Legacy Calendar event is not an owned callback"
                    )
            else:
                self._assert_owner(existing.json(), owner)
        exists = existing.status_code not in {404, 410}
        if task.status != "open":
            if exists:
                deleted = self._request("DELETE", path, timeout=20)
                if deleted.status_code not in {200, 204, 404, 410}:
                    deleted.raise_for_status()
                verified = self._request("GET", path, timeout=20)
                if verified.status_code not in {404, 410}:
                    raise CalendarProjectionError("Calendar deletion not confirmed")
            return {
                "provider": "google_calendar",
                "calendar_id": self.calendar_id,
                "event_id": event_id,
                "status": "deleted",
                "verified": True,
            }
        zone = ZoneInfo(self.timezone)
        start = task.due_at.astimezone(zone)
        payload = {
            "summary": task.title,
            "description": "Callback registado no CRM. " + f"Task {task.id}",
            "start": {"dateTime": start.isoformat(), "timeZone": self.timezone},
            "end": {
                "dateTime": (
                    start + timedelta(minutes=self.duration_minutes)
                ).isoformat(),
                "timeZone": self.timezone,
            },
            "extendedProperties": {"private": owner | {"pt_logistics_callback": "1"}},
        }
        if exists:
            response = self._request("PATCH", path, json=payload, timeout=20)
        else:
            response = self._request(
                "POST", collection, json=payload | {"id": event_id}, timeout=20
            )
            if response.status_code == 409:
                conflict = self._request("GET", path, timeout=20)
                conflict.raise_for_status()
                self._assert_owner(conflict.json(), owner)
                response = self._request("PATCH", path, json=payload, timeout=20)
        response.raise_for_status()
        readback = self._request("GET", path, timeout=20)
        readback.raise_for_status()
        observed = readback.json()
        self._assert_owner(observed, owner)
        from datetime import datetime

        observed_start = datetime.fromisoformat(
            observed.get("start", {}).get("dateTime", "").replace("Z", "+00:00")
        )
        if observed_start != task.due_at or observed.get("summary") != task.title:
            raise CalendarProjectionError("Calendar callback verification failed")
        return {
            "provider": "google_calendar",
            "calendar_id": self.calendar_id,
            "event_id": event_id,
            "status": "scheduled",
            "verified": True,
            "due_at": task.due_at.isoformat(),
        }

    @staticmethod
    def _assert_owner(event, owner):
        actual = event.get("extendedProperties", {}).get("private", {})
        if any(actual.get(key) != value for key, value in owner.items()):
            raise CalendarProjectionError("Calendar event belongs to another workflow")


def calendar_from_environment():
    return CanonicalCallbackCalendar(
        credentials_file=os.environ.get("GOOGLE_CALENDAR_CREDENTIALS_FILE")
        or os.environ.get("GOOGLE_CREDENTIALS_FILE", ""),
        calendar_id=os.environ.get("CALLBACK_CALENDAR_ID", ""),
        timezone=os.environ.get("APP_TIMEZONE", "Europe/Lisbon"),
    )


def execute_callback(session, workspace_id, work_id, lease_token, *, calendar=None):
    row = locked_work(session, workspace_id, work_id)
    if row.status == "completed" and row.last_lease_token == lease_token:
        return {
            "replayed": True,
            "result": row.result,
            "status": row.status,
            "id": str(row.id),
        }
    validate_lease(row, lease_token)
    if row.kind != "calendar_callback" or row.task_id is None:
        raise CalendarProjectionError("Work requires agent review")
    task = session.scalar(
        select(Task)
        .where(Task.workspace_id == workspace_id, Task.id == row.task_id)
        .with_for_update()
    )
    if task is None:
        raise CalendarProjectionError("Callback task is unavailable")
    legacy_event_id = ""
    if task.source_rule == "release:legacy_callback" and task.lead_id:
        lead = session.scalar(
            select(Lead).where(
                Lead.workspace_id == workspace_id, Lead.id == task.lead_id
            )
        )
        if lead and lead.source_identity_id:
            source = session.scalar(
                select(SourceIdentity).where(
                    SourceIdentity.workspace_id == workspace_id,
                    SourceIdentity.id == lead.source_identity_id,
                )
            )
            if source:
                legacy_event_id = str(
                    (source.metadata_json or {}).get("calendar_event_id") or ""
                )
    evidence = (calendar or calendar_from_environment()).sync_task(
        task, legacy_event_id=legacy_event_id
    )
    return finish_work(
        session,
        workspace_id,
        work_id,
        lease_token,
        result={"summary": "Callback confirmado no Calendar", "evidence": [evidence]},
    )
