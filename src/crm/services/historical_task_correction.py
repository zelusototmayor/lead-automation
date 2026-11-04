"""Cited correction of an existing overdue callback date; never new scheduling."""
from datetime import UTC, date, datetime, time, timedelta
import re
from typing import Literal
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.crm.activity_provenance import operational_activity_filter
from src.crm.persistence.models import Activity, AuditEvent, Task
from src.crm.services.agent_work_service import WorkConflict, lead_is_suppressed
from src.crm.services.jev_note_policy import digest
from src.crm.services.note_source_service import source_digest

ACTION = 'task.historical_due_date_corrected'
ZONE = ZoneInfo('Europe/Lisbon')
WEEKDAYS = ('segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo')


class HistoricalDueDateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: UUID
    lead_id: UUID
    expected_lead_version: int = Field(ge=1, strict=True)
    expected_task_version: int = Field(ge=1, strict=True)
    expected_due_at: AwareDatetime
    corrected_due_at: AwareDatetime
    source_activity_id: UUID
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    quote: str = Field(min_length=1, max_length=400)
    documented_weekday: Literal['segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo']
    documented_local_time: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    timezone: Literal['Europe/Lisbon']
    reviewed_on: date
    review_ref: str = Field(min_length=1, max_length=160)
    linkage_basis: Literal['operator_reviewed_same_obligation']


def _now():
    return datetime.now(UTC)


def _documented_due(source, body):
    # Support only an explicitly quoted next weekday and clock time. Arbitrary
    # date overrides and ambiguous same-weekday language are deliberately absent.
    hour, minute = map(int, body.documented_local_time.split(':'))
    quote = body.quote.casefold()
    quoted_days = {day for day in WEEKDAYS if re.search(r'\b' + re.escape(day) + r'(?=\W|\d|$)', quote)}
    quoted_times = {(int(h), int(m)) for h, m in re.findall(
        r'(?<!\d)([01]?\d|2[0-3])(?:h|:)([0-5]\d)(?!\d)', quote)}
    if quoted_days != {body.documented_weekday} or quoted_times != {(hour, minute)}:
        raise WorkConflict('One explicit cited weekday and local time required')
    local = source.occurred_at.astimezone(ZONE)
    days = (WEEKDAYS.index(body.documented_weekday) - local.weekday()) % 7
    if days == 0:
        raise WorkConflict('Same-weekday reference is ambiguous')
    documented = datetime.combine(local.date() + timedelta(days=days), time(hour, minute), ZONE)
    if documented.replace(fold=0).utcoffset() != documented.replace(fold=1).utcoffset():
        raise WorkConflict('Ambiguous local clock time')
    return documented.astimezone(UTC)


def correct_historical_due_date(session, principal, task_id, body):
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    lead, _ = _locked_company_lead(session, principal.workspace_id, body.lead_id)
    task = session.scalar(select(Task).where(Task.workspace_id == principal.workspace_id,
        Task.id == task_id, Task.lead_id == lead.id).with_for_update())
    if task is None:
        raise WorkConflict('Task unavailable')
    request = body.model_dump(mode='json'); request_hash = digest(request)
    identity = uuid5(body.command_id, 'historical-task-date:' + str(principal.workspace_id))
    prior = session.scalar(select(AuditEvent).where(AuditEvent.workspace_id == principal.workspace_id,
                                                  AuditEvent.command_id == body.command_id))
    # Replay is checked under the task lock before the expected old version.
    if prior is not None:
        result = (prior.details or {}).get('result', {})
        if (prior.id != identity or prior.action != ACTION or prior.entity_id != task.id
                or prior.actor_id != principal.actor_id or prior.details.get('request_hash') != request_hash
                or task.version != result.get('version') or task.due_at != body.corrected_due_at
                or task.status != 'open'):
            raise WorkConflict('Historical correction command changed')
        return result | {'replayed': True}
    now = _now()
    if (body.reviewed_on != now.astimezone(ZONE).date() or not body.review_ref.strip()
            or lead.version != body.expected_lead_version or lead_is_suppressed(session, lead)
            or task.version != body.expected_task_version or task.due_at != body.expected_due_at
            or task.status != 'open' or task.task_type != 'call' or task.completed_at is not None
            or not task.due_at < now or not body.corrected_due_at < now
            or task.due_at == body.corrected_due_at):
        raise WorkConflict('Current overdue task and exact review required')
    _locked_rows(session, select(Activity).where(Activity.workspace_id == principal.workspace_id,
                                                Activity.lead_id == lead.id))
    source = session.scalar(select(Activity).where(Activity.workspace_id == principal.workspace_id,
        Activity.id == body.source_activity_id, Activity.lead_id == lead.id,
        Activity.activity_type.in_(('call', 'note')), Activity.actor_type == 'human', operational_activity_filter()))
    if (source is None or not source.summary or not body.quote.strip() or body.quote not in source.summary
            or source_digest(source) != body.source_digest
            or session.scalar(select(Activity.id).where(Activity.workspace_id == principal.workspace_id,
                Activity.supersedes_activity_id == source.id).limit(1))
            or not source.occurred_at < body.corrected_due_at
            or body.corrected_due_at != _documented_due(source, body)):
        raise WorkConflict('Current cited historical instruction required')
    previous = {'due_at': task.due_at.astimezone(UTC).isoformat(), 'version': task.version}
    task.due_at = body.corrected_due_at.astimezone(UTC)
    session.flush()
    result = {'status': 'historical_date_corrected', 'task_id': str(task.id), 'version': task.version,
        'due_at': task.due_at.isoformat(), 'task_status': task.status, 'audit_id': str(identity),
        'source_activity_id': str(source.id), 'source_digest': body.source_digest,
        'effects_performed': ['existing_task_historical_due_date_corrected'],
        'calendar_effects': [], 'new_tasks': 0, 'new_contact_agreement': False}
    session.add(AuditEvent(id=identity, workspace_id=principal.workspace_id, command_id=body.command_id,
        actor_id=principal.actor_id, action=ACTION, entity_type='task', entity_id=task.id,
        details={'version': 'historical-task-date/v1', 'request': request, 'request_hash': request_hash,
                 'previous': previous, 'result': result}))
    session.flush()
    return result | {'replayed': False}
