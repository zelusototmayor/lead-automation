"""Deterministic, owner-checked Calendar projection of one independent call task."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
import os
from urllib.parse import quote
from zoneinfo import ZoneInfo
from uuid import UUID

from sqlalchemy import select
from src.crm.callback_calendar import CallbackCalendar
from src.crm.persistence.models import Task, Lead, SourceIdentity, Activity, AuditEvent
from src.crm.services.agent_work_service import locked_work, validate_lease, finish_work


class CalendarProjectionError(RuntimeError):
    pass


class CanonicalCallbackCalendar(CallbackCalendar):
    """A provider retry uses the same event ID, even after an uncertain POST."""

    def sync_task(self, task, *, legacy_event_id="", legacy_proof=None,
                  company_name="", call_notes="", content_only=False):
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
                self._assert_legacy_owner(
                    existing.json(), owner, event_id, legacy_proof
                )
            else:
                self._assert_owner(existing.json(), owner)
        exists = existing.status_code not in {404, 410}
        before = existing.json() if exists else {}
        if content_only:
            private = (before.get("extendedProperties") or {}).get("private") or {}
            if (not exists or task.status != "open" or legacy_event_id
                    or event_id != canonical_id or before.get("id") != canonical_id
                    or private.get("pt_logistics_callback") != "1"):
                raise CalendarProjectionError("Content repair requires an existing marked canonical callback")
            self._assert_owner(before, owner)
        if exists and before.get("attendees") and task.status == "open":
            raise CalendarProjectionError("Callback notes cannot be published to attendees")
        already_cancelled = exists and existing.json().get("status") == "cancelled"
        if task.status != "open":
            if exists and not already_cancelled:
                deleted = self._request("DELETE", path, timeout=20)
                if deleted.status_code not in {200, 204, 404, 410}:
                    deleted.raise_for_status()
                verified = self._request("GET", path, timeout=20)
                if verified.status_code not in {404, 410}:
                    verified.raise_for_status()
                    tombstone = verified.json()
                    self._assert_owner(tombstone, owner)
                    if tombstone.get("status") != "cancelled":
                        raise CalendarProjectionError("Calendar deletion not confirmed")
            return {
                "provider": "google_calendar",
                "calendar_id": self.calendar_id,
                "event_id": event_id,
                "status": "deleted",
                "verified": True,
            }
        if already_cancelled:
            raise CalendarProjectionError(
                "Calendar callback was cancelled; review required"
            )
        zone = ZoneInfo(self.timezone)
        start = task.due_at.astimezone(zone)
        company_name = (company_name or "").strip()
        if not company_name:
            raise CalendarProjectionError("Callback company is unavailable")
        description = (call_notes or "").strip()
        if not description and legacy_event_id and exists:
            previous = existing.json().get("description", "")
            if not previous.startswith("Callback registado no CRM. Task "):
                description = previous
        payload = {
            "summary": company_name,
            "description": description or "Sem notas de chamada registadas no CRM.",
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
            if content_only:
                payload = {key: payload[key] for key in ("summary", "description")}
            if all(before.get(key) == value for key, value in payload.items()):
                response = existing
            else:
                response = self._request(
                    "PATCH", path, json=payload, params={"sendUpdates": "none"},
                    headers={"If-Match": before["etag"]} if before.get("etag") else {},
                    timeout=20,
                )
        else:
            response = self._request(
                "POST", collection, json=payload | {"id": event_id}, params={"sendUpdates": "none"}, timeout=20
            )
            if response.status_code == 409:
                conflict = self._request("GET", path, timeout=20)
                conflict.raise_for_status()
                self._assert_owner(conflict.json(), owner)
                response = self._request("PATCH", path, json=payload, params={"sendUpdates": "none"}, timeout=20)
        response.raise_for_status()
        readback = self._request("GET", path, timeout=20)
        readback.raise_for_status()
        observed = readback.json()
        self._assert_owner(observed, owner)
        if content_only:
            preserved = ("id", "iCalUID", "start", "end", "attendees", "organizer",
                         "creator", "status", "recurrence", "recurringEventId",
                         "originalStartTime", "location", "reminders", "conferenceData",
                         "extendedProperties")
            if any(observed.get(key) != before.get(key) for key in preserved):
                raise CalendarProjectionError("Calendar content repair changed protected fields")
        from datetime import datetime

        observed_start = datetime.fromisoformat(
            observed.get("start", {}).get("dateTime", "").replace("Z", "+00:00")
        )
        if (
            (not content_only and observed_start != task.due_at)
            or observed.get("summary") != payload["summary"]
            or observed.get("description") != payload["description"]
        ):
            raise CalendarProjectionError("Calendar callback verification failed")
        return {
            "provider": "google_calendar",
            "calendar_id": self.calendar_id,
            "event_id": event_id,
            "status": "scheduled",
            "verified": True,
            "due_at": task.due_at.isoformat(),
        }

    def _assert_legacy_owner(self, event, owner, event_id, proof=None):
        private = (event.get("extendedProperties") or {}).get("private") or {}
        if any(private.get(key) not in (None, value) for key, value in owner.items()):
            raise CalendarProjectionError("Legacy Calendar event has another CRM owner")
        if private.get("pt_logistics_callback") == "1":
            return
        # Pre-marker dashboard events can be adopted only through frozen import
        # provenance plus the exact historical event template and original date.
        try:
            company = proof["company"]
            description = event.get("description", "").strip().splitlines()
            observed_start = datetime.fromisoformat(
                event["start"]["dateTime"].replace("Z", "+00:00")
            )
            observed_end = datetime.fromisoformat(
                event["end"]["dateTime"].replace("Z", "+00:00")
            )
            valid = (
                proof["event_id"] == event_id == event.get("id")
                and bool(re.fullmatch(r"[0-9a-f]{64}", proof["snapshot_sha256"]))
                and proof["source_scope"].endswith(":PT Logistics")
                and company
                and event.get("summary") == "Call: " + company
                and description[0] == "Company: " + company
                and description[-1]
                == "Created from the PT Logistics dashboard callback workflow."
                and observed_start == proof["due_at"]
                and observed_end - observed_start == timedelta(minutes=10)
                and event.get("organizer", {}).get("email") == self.calendar_id
                and event.get("status") == "confirmed"
                and not event.get("attendees")
            )
        except (TypeError, KeyError, IndexError, ValueError):
            valid = False
        if not valid:
            raise CalendarProjectionError(
                "Legacy Calendar event is not a proven callback"
            )

    @staticmethod
    def _assert_owner(event, owner):
        actual = (event.get("extendedProperties") or {}).get("private") or {}
        if any(actual.get(key) != value for key, value in owner.items()):
            raise CalendarProjectionError("Calendar event belongs to another workflow")


def legacy_proof_from_source(source, lead):
    """Only canonical frozen Sheets rows can authorize pre-marker adoption."""
    metadata = source.metadata_json or {}
    row = metadata.get("legacy_row") or {}
    if (
        source.source_system != "google_sheets"
        or source.canonical_entity_type != "lead"
        or source.canonical_entity_id != lead.id
        or metadata.get("suppressed")
        or not row.get("Due Time")
        or row.get("Calendar Event ID") != metadata.get("calendar_event_id")
    ):
        return None
    for date_format in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            due = datetime.strptime(
                row["Due"] + " " + row["Due Time"], date_format + " %H:%M"
            ).replace(tzinfo=ZoneInfo("Europe/Lisbon"))
            return {
                "company": row["Company"],
                "event_id": row["Calendar Event ID"],
                "due_at": due,
                "snapshot_sha256": metadata.get("snapshot_sha256", ""),
                "source_scope": source.source_scope,
            }
        except (KeyError, TypeError, ValueError):
            continue
    return None


def callback_content(session, task):
    """Company from the current lead; notes from the call which created the task.

    Never substitute a task title for missing notes, or use later/unrelated calls.
    Legacy descriptions remain intact when no canonical call is linked.
    """
    lead = session.scalar(select(Lead).where(
        Lead.workspace_id == task.workspace_id, Lead.id == task.lead_id
    ))
    company = (lead.company_name or "").strip() if lead else ""
    if not company:
        raise CalendarProjectionError("Callback company is unavailable")
    activity = None
    if task.source_rule == "human_call_callback":
        audit = session.scalar(select(AuditEvent).where(
            AuditEvent.workspace_id == task.workspace_id,
            AuditEvent.entity_id == task.lead_id,
            AuditEvent.action == "lead.call_logged",
            AuditEvent.details["task_id"].astext == str(task.id),
        ))
        activity_id = (audit.details or {}).get("activity_id") if audit else None
        if activity_id:
            activity = session.scalar(select(Activity).where(
                Activity.workspace_id == task.workspace_id,
                Activity.lead_id == task.lead_id,
                Activity.id == UUID(activity_id),
                Activity.activity_type == "call",
            ))
    elif task.source_rule == "manual_next_action":
        activity = session.scalar(select(Activity).where(
            Activity.workspace_id == task.workspace_id,
            Activity.lead_id == task.lead_id,
            Activity.activity_type == "call",
            Activity.occurred_at <= task.created_at,
            Activity.created_at <= task.created_at,
        ).order_by(Activity.occurred_at.desc(), Activity.id.desc()).limit(1))
    return {"company_name": company,
            "call_notes": (activity.summary or "").strip() if activity else ""}


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
    legacy_proof = None
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
                legacy_proof = legacy_proof_from_source(source, lead)
    content = callback_content(session, task) if task.status == "open" else {}
    evidence = (calendar or calendar_from_environment()).sync_task(
        task, legacy_event_id=legacy_event_id, legacy_proof=legacy_proof,
        company_name=content.get("company_name", ""),
        call_notes=content.get("call_notes", ""),
    )
    return finish_work(
        session,
        workspace_id,
        work_id,
        lease_token,
        result={"summary": "Callback confirmado no Calendar", "evidence": [evidence]},
    )
