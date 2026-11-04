"""Durable, append-only Calendar projection of one independent callback intent."""

from __future__ import annotations

from datetime import datetime
import os
from zoneinfo import ZoneInfo
from uuid import UUID

from sqlalchemy import select
from src.crm.callback_calendar import CallbackCalendar
from src.crm.persistence.models import Task, Lead, Account, Activity, AuditEvent
from src.crm.services.agent_work_service import locked_work, validate_lease, finish_work


class CalendarProjectionError(RuntimeError):
    pass


class CanonicalCallbackCalendar(CallbackCalendar):
    """Only POST for a new occurrence; no historical update/delete path."""

    def sync_task(self, task, **kwargs):
        from src.crm.services.callback_projection import sync_task
        return sync_task(self, task, **kwargs)

    @staticmethod
    def _assert_owner(event, owner):
        actual = (event.get("extendedProperties") or {}).get("private") or {}
        if any(actual.get(key) != value for key, value in owner.items()):
            raise CalendarProjectionError("Calendar event belongs to another workflow")


def legacy_proof_from_source(source, lead):
    """Read-only legacy provenance; never authorizes a history mutation."""
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
    """Company and full original source note, not a title or later call."""
    lead = session.scalar(select(Lead).where(
        Lead.workspace_id == task.workspace_id, Lead.id == task.lead_id
    ))
    account = session.scalar(select(Account).where(Account.workspace_id == task.workspace_id,
        Account.id == lead.account_id)) if lead and lead.account_id else None
    company = (lead.company_name or '').strip() if lead else ''
    if not company and account:
        company = (account.display_name or '').strip()
    if not company:
        raise CalendarProjectionError("Callback company is unavailable")
    from src.crm.services.note_recovery_content import recovery_content
    recovered = recovery_content(session, task)
    if recovered is not None:
        return {"company_name": company, "call_notes": recovered}
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
    elif task.source_rule == "head_of_sales_note":
        audit = session.scalar(select(AuditEvent).where(
            AuditEvent.workspace_id == task.workspace_id,
            AuditEvent.action == 'agent.note_interpreted',
            AuditEvent.details['task_ids'].contains([str(task.id)]))
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1))
        if audit:
            activity = session.scalar(select(Activity).where(
                Activity.workspace_id == task.workspace_id,
                Activity.lead_id == task.lead_id, Activity.id == audit.entity_id))
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
        duration_minutes=15,
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
    payload = row.payload or {}
    if 'terminal_note_cancellation_v1' in payload:
        from src.crm.services.call_note_calendar_cancellation import execute_note_cancellation
        result = execute_note_cancellation(session, row, task,
            calendar or calendar_from_environment())
        return finish_work(session, workspace_id, work_id, lease_token, result=result)
    occurrence = payload.get('callback_occurrence')
    if task.status == 'open' and payload.get('task_version') != task.version:
        return finish_work(session, workspace_id, work_id, lease_token,
            result={'summary': 'Superseded unprojected work; historical events unchanged.',
                    'evidence': [{'status': 'superseded', 'verified': True}]})
    if task.status == 'open' and (not occurrence or occurrence.get('migration_required')):
        raise CalendarProjectionError('Pre-cutover callback requires exact migration evidence')
    content = (occurrence or {}).get('content') or (
        callback_content(session, task) if task.status == 'open' else {})
    evidence = (calendar or calendar_from_environment()).sync_task(
        task, occurrence=occurrence, **content)
    if evidence.get('status') == 'scheduled' and occurrence:
        row.payload = dict(payload, callback_occurrence=dict(occurrence, projected=True))
    return finish_work(
        session, workspace_id, work_id, lease_token,
        result={"summary": "Retorno adicionado ao calendário." if task.status == "open"
                else "Histórico do calendário preservado.", "evidence": [evidence]},
    )
