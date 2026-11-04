"""Retire only future, unchanged CRM-owned callbacks for a bound human closure.

Runs inside the existing calendar_callback worker and durable work receipt.
An uncertain DELETE is reconciled with GET on the next execution, never assumed
successful. Past events, meetings, invitations and human edits are preserved.
"""
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import select
from src.crm.persistence.models import Activity, AgentWork, AuditEvent, Lead
from src.crm.services.note_source_service import source_digest
from src.crm.services.call_note_alignment import CANCELLATION_KEY, TERMINAL


def _iso(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Aware Calendar time required')
    return parsed.astimezone(UTC)


def cancel_owned_future_event(calendar, task, occurrence, *, now=None):
    from src.crm.services.callback_execution import CalendarProjectionError
    now = now or datetime.now(UTC)
    event_id = occurrence.get('event_id')
    if not event_id or not calendar.configured():
        raise CalendarProjectionError('Callback cancellation configuration unavailable')
    due = _iso(occurrence['due_at'])
    evidence = {'provider': 'google_calendar', 'calendar_id': calendar.calendar_id,
                'event_id': event_id, 'task_id': str(task.id), 'email_sent': False}
    if due <= now:
        return dict(evidence, status='history_preserved', verified=True)
    path = '/calendars/' + quote(calendar.calendar_id, safe='') + '/events/' + quote(event_id, safe='')
    response = calendar._request('GET', path, timeout=20)
    if response.status_code in (404, 410):
        return dict(evidence, status='cancelled', verified=True, reconciliation='already_absent')
    response.raise_for_status()
    event = response.json()
    if event.get('id') != event_id:
        raise CalendarProjectionError('Callback cancellation identity conflict')
    # Google may return a minimal tombstone without extendedProperties. The
    # canonical original occurrence already binds this exact ID; no write is
    # needed when its cancellation is observed directly.
    if event.get('status') == 'cancelled':
        return dict(evidence, status='cancelled', verified=True, reconciliation='already_cancelled')
    private = (event.get('extendedProperties') or {}).get('private') or {}
    required = {'crm_task_id': str(task.id), 'crm_workspace_id': str(task.workspace_id),
                'crm_occurrence_id': event_id, 'pt_logistics_callback': '1'}
    if any(private.get(k) != v for k, v in required.items()):
        raise CalendarProjectionError('Callback cancellation ownership conflict')
    if event.get('attendees') or event.get('recurrence') or event.get('recurringEventId'):
        raise CalendarProjectionError('Only independent private callbacks may be cancelled')
    try:
        start = _iso(event['start']['dateTime'])
        end = _iso(event['end']['dateTime'])
    except (KeyError, TypeError, ValueError):
        raise CalendarProjectionError('Callback cancellation date conflict') from None
    content = occurrence.get('content') or {}
    description = (content.get('call_notes') or '').strip() or 'Sem notas de chamada registadas no CRM.'
    if (start != due or end <= start or (end - start).total_seconds() != 900
            or not content.get('company_name') or event.get('summary') != 'call: ' + content['company_name']
            or event.get('description') != description or not event.get('etag')):
        raise CalendarProjectionError('Callback changed; human Calendar edits preserved')
    if start <= now:
        return dict(evidence, status='history_preserved', verified=True)
    deleted = calendar._request('DELETE', path, headers={'If-Match': event['etag']},
                               params={'sendUpdates': 'none'}, timeout=20)
    if deleted.status_code not in (204, 404, 410):
        deleted.raise_for_status()
        raise CalendarProjectionError('Callback cancellation was not acknowledged')
    readback = calendar._request('GET', path, timeout=20)
    if readback.status_code in (404, 410):
        return dict(evidence, status='cancelled', verified=True)
    readback.raise_for_status()
    if readback.json().get('id') == event_id and readback.json().get('status') == 'cancelled':
        return dict(evidence, status='cancelled', verified=True)
    raise CalendarProjectionError('Callback cancellation not verified')


def execute_note_cancellation(session, row, task, calendar):
    from src.crm.services.callback_execution import CalendarProjectionError
    marker = (row.payload or {}).get(CANCELLATION_KEY) or {}
    lead = session.scalar(select(Lead).where(Lead.workspace_id == row.workspace_id,
        Lead.id == row.lead_id).with_for_update())
    try:
        source = session.get(Activity, UUID(marker['source_activity_id']))
        audit = session.get(AuditEvent, UUID(marker['task_audit_id']))
        original = session.get(AgentWork, UUID(marker['original_work_id']))
    except (KeyError, ValueError, TypeError):
        raise CalendarProjectionError('Invalid callback cancellation binding') from None
    if (lead is None or lead.stage not in TERMINAL or lead.stage != marker.get('stage')
            or task.status != 'cancelled' or task.version != marker.get('task_version')
            or source is None or source.workspace_id != row.workspace_id or source.lead_id != row.lead_id
            or source.actor_type != 'human' or source.source_system != 'manual'
            or source_digest(source) != marker.get('source_digest')
            or audit is None or audit.workspace_id != row.workspace_id or audit.entity_id != task.id
            or audit.action != 'task.cancelled_from_call_note'
            or (audit.details or {}).get('source_digest') != marker.get('source_digest')
            or (audit.details or {}).get('cancelled_version') != task.version
            or original is None or original.workspace_id != row.workspace_id or original.task_id != task.id
            or original.kind != 'calendar_callback'
            or (original.payload or {}).get('callback_occurrence') != marker.get('occurrence')):
        raise CalendarProjectionError('Callback cancellation source or task changed')
    evidence = cancel_owned_future_event(calendar, task, marker['occurrence'])
    evidence.update(source_activity_id=str(source.id), source_digest=marker['source_digest'])
    return {'summary': 'Retorno futuro cancelado por encerramento explícito da lead.'
            if evidence['status'] == 'cancelled' else 'Histórico passado do calendário preservado.',
            'disposition': 'cancelled', 'commercial_execution': False, 'email_sent': False,
            'evidence': [evidence]}
