"""Cancel superseded commercial obligations from an explicit human closure.

Caller owns the lead lock, current-source check and transaction. This module
does not send email, complete commercial tasks or remove Calendar history.
Future owned callbacks are cancelled by the existing Calendar worker.
"""
from datetime import UTC, datetime
import hashlib
import json
from uuid import uuid5

from sqlalchemy import select, and_, or_

from src.crm.persistence.models import Activity, AgentWork, AuditEvent, Task
from src.crm.services.agent_work_service import enqueue_work
from src.crm.services.command_service import HumanCommandPrincipal
from src.crm.services.note_source_service import source_digest
from src.crm.services.task_command_service import CancelTaskCommand, TaskCommandService
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork

POLICY = 'explicit-human-note-action-alignment/2026-10-30-v1'
CANCELLATION_KEY = 'terminal_note_cancellation_v1'
TERMINAL = frozenset(('lost', 'not_a_fit', 'won'))
MEETING_OUTREACH_RULES = ('manual_next_action', 'ordinary-followup/v1',
                         'head_of_sales_note', 'head_of_sales', 'human_call_callback')


def superseded_action_filters(workspace_id, lead_id, source, target_stage):
    """Same bounded task predicate for the writer and read-only verification."""
    filters = [Task.workspace_id == workspace_id, Task.lead_id == lead_id,
        Task.status == 'open', Task.task_type.in_(('call', 'email', 'follow_up')),
        Task.created_at <= source.created_at, Task.updated_at <= source.created_at]
    if target_stage == 'meeting_booked':
        filters.extend((Task.due_at <= source.occurred_at, Task.proposal_id.is_(None),
            Task.source_rule.in_(MEETING_OUTREACH_RULES),
            or_(Task.task_type.in_(('call', 'follow_up')),
                and_(Task.task_type == 'email', Task.source_rule == 'ordinary-followup/v1'))))
    elif target_stage not in TERMINAL:
        raise ValueError('Stage has no automatic task cancellation policy')
    return filters


def _future_occurrence(work, now):
    occurrence = (work.payload or {}).get('callback_occurrence') or {}
    try:
        due = datetime.fromisoformat(occurrence['due_at'])
        future = due.tzinfo is not None and due.astimezone(UTC) > now
    except (KeyError, ValueError, TypeError):
        return None
    projected = occurrence.get('projected') is True or any(
        e.get('provider') == 'google_calendar' and e.get('verified') is True
        and e.get('event_id') == occurrence.get('event_id') and e.get('status') == 'scheduled'
        for e in (work.result or {}).get('evidence', []))
    return dict(occurrence) if future and projected and occurrence.get('event_id') else None


def cancel_open_commercial_tasks(session, lead, source, actor_id, *, retained_callback_task_ids=()):
    if (lead.stage not in TERMINAL | {'meeting_booked'} or source.workspace_id != lead.workspace_id
            or source.lead_id != lead.id or source.actor_type != 'human'
            or source.source_system != 'manual' or source.actor_id is None):
        raise ValueError('Explicit human stage source required')
    # Attribute the original human decision to its actual author, never a
    # model-provided name or a caller trying to adopt somebody else's identity.
    author = source.actor_id
    retained = frozenset(str(value) for value in retained_callback_task_ids)
    if retained:
        from src.crm.services.call_note_stage import retained_callback_intent
        if not retained_callback_intent(source):
            raise ValueError('Retained callback requires explicit current human instruction')
    digest = source_digest(source)
    now = datetime.now(UTC)
    reason = ('meeting_booked_supersedes_overdue_outreach' if lead.stage == 'meeting_booked'
              else 'explicit_human_terminal_stage')
    result = {'cancelled_task_ids': [], 'cancelled_work_ids': [],
              'calendar_cancellation_work_ids': [], 'preserved_newer_task_ids': [],
              'preserved_current_callback_task_ids': [],
              'in_flight_work_ids': [], 'commercial_tasks_completed': False,
              'email_sent': False}
    task_filters = (superseded_action_filters(lead.workspace_id, lead.id, source, lead.stage)
        if lead.stage == 'meeting_booked' else [Task.workspace_id == lead.workspace_id,
            Task.lead_id == lead.id, Task.status == 'open',
            Task.task_type.in_(('call', 'email', 'follow_up'))])
    tasks = list(session.scalars(select(Task).where(*task_filters)
        .order_by(Task.id).with_for_update()))
    uow = SqlAlchemyUnitOfWork(lambda: session).__enter__()
    principal = HumanCommandPrincipal(author, lead.workspace_id,
                                      frozenset(('crm:task:write',)))
    for task in tasks:
        # The trusted note-plan caller has already bound these prepared call
        # tasks to this source's exact callback quote. PG transaction timestamps
        # can be equal when the UI creates its next action before the call row.
        if str(task.id) in retained and task.task_type == 'call':
            result['preserved_current_callback_task_ids'].append(str(task.id))
            continue
        if task.created_at > source.created_at or task.updated_at > source.created_at:
            result['preserved_newer_task_ids'].append(str(task.id))
            continue
        command_id = uuid5(source.id, POLICY + ':cancel-task:' + str(task.id))
        previous_version = task.version
        TaskCommandService(uow).cancel(principal, CancelTaskCommand(
            command_id, lead.workspace_id, task.id, task.version))
        # TaskCommandService owns status, version, Activity, Audit and outbox.
        if task.status != 'cancelled':
            raise ValueError('Task cancellation was not applied')
        audit_id = uuid5(command_id, 'source-binding')
        session.add(AuditEvent(id=audit_id, workspace_id=lead.workspace_id,
            command_id=audit_id, actor_id=author, action='task.cancelled_from_call_note',
            entity_type='task', entity_id=task.id,
            details={'source_activity_id': str(source.id), 'source_digest': digest,
                     'lead_id': str(lead.id), 'stage': lead.stage,
                     'cancellation_command_id': str(command_id),
                     'reason': reason,
                     'previous_version': previous_version, 'cancelled_version': task.version,
                     'policy': POLICY, 'commercial_execution': False, 'email_sent': False}))
        result['cancelled_task_ids'].append(str(task.id))
        works = list(session.scalars(select(AgentWork).where(
            AgentWork.workspace_id == lead.workspace_id, AgentWork.task_id == task.id,
            AgentWork.kind.in_(('followup_due', 'calendar_callback'))
        ).order_by(AgentWork.id).with_for_update()))
        seen_events = set()
        for work in works:
            if CANCELLATION_KEY in (work.payload or {}):
                continue
            occurrence = (_future_occurrence(work, now)
                if work.kind == 'calendar_callback' and lead.stage in TERMINAL else None)
            if occurrence and occurrence['event_id'] not in seen_events:
                seen_events.add(occurrence['event_id'])
                marker = {'source_activity_id': str(source.id), 'source_digest': digest,
                          'task_version': task.version, 'task_audit_id': str(audit_id),
                          'original_work_id': str(work.id), 'stage': lead.stage,
                          'occurrence': occurrence}
                cancellation = enqueue_work(session, workspace_id=lead.workspace_id,
                    source_key='callback-cancel:' + str(source.id) + ':' + str(task.id)
                        + ':' + occurrence['event_id'], kind='calendar_callback',
                    task_id=task.id, lead_id=lead.id, wake_note_worker=True,
                    payload={'task_version': task.version, CANCELLATION_KEY: marker})
                result['calendar_cancellation_work_ids'].append(str(cancellation))
            if work.status == 'running':
                result['in_flight_work_ids'].append(str(work.id))
                continue
            if work.status not in ('queued', 'waiting', 'failed'):
                continue
            # AgentWork has no cancelled DB state. Its technical disposition is
            # complete, with explicit cancellation evidence; the actual task is
            # CANCELLED and no delivery/call completion is claimed.
            work.result = {'summary': ('Contacto inicial antigo cancelado pela reunião já marcada; não executado.'
                    if lead.stage == 'meeting_booked' else
                    'Ação cancelada pela indicação humana de encerramento da lead; não executada.'),
                'disposition': 'cancelled', 'task_status': 'cancelled',
                'reason': reason,
                'email_sent': False, 'commercial_execution': False,
                'evidence': [{'activity_id': str(source.id), 'source_digest': digest,
                              'task_id': str(task.id), 'audit_id': str(audit_id)}]}
            encoded = json.dumps({'status': 'completed', 'result': work.result},
                sort_keys=True, separators=(',', ':'), allow_nan=False)
            work.status = 'completed'
            work.result_hash = hashlib.sha256(encoded.encode()).hexdigest()
            work.error = None
            work.updated_at = now
            result['cancelled_work_ids'].append(str(work.id))
    session.flush()
    return result
