"""Source-bound email state updates; no sends, drafts or calendar mutations."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from src.crm.persistence.models import Activity, AgentWork, AuditEvent, Lead, Task
from src.crm.services.agent_work_service import (
    WorkConflict, enqueue_work, finish_work, locked_work, serialize_work, validate_lease,
)
from src.crm.domain.stage_policy import highest_stage_rank, is_terminal_stage, requires_account, stage_rank


class EmailUpdateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lease_token: UUID
    expected_lead_version: int = Field(ge=1)
    source_digest: str = Field(pattern='^[a-f0-9]{64}$')
    summary: str = Field(min_length=1, max_length=240)
    target_stage: Literal['contacted','qualified','meeting_booked','meeting_held',
        'proposal_requested','proposal_sent','negotiation','won','lost','not_a_fit'] | None = None
    quote: str | None = Field(default=None, max_length=1800)

    @field_validator('summary')
    @classmethod
    def short_note(cls, value):
        if value != value.strip() or len(value.splitlines()) != 1:
            raise ValueError('Use one short line')
        return value


def current_text(text):
    """Exclude quoted/forwarded history from evidence for a new state change."""
    lines = []
    for line in (text or '').splitlines():
        if re.match(r'^\s*(?:>|On .+wrote:|Em .+escreveu:|From:|De:|'
                    r'-{2,}\s*(?:Original Message|Mensagem original|Forwarded message|Mensagem encaminhada))', line, re.I):
            break
        if re.match(r'^\s*--\s*$', line):
            break
        lines.append(line)
    return '\n'.join(lines).strip()


def active(occurred):
    value = os.environ.get('CRM_EMAIL_UPDATES_FROM')
    if not value:
        return False
    cutoff = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return cutoff.tzinfo is not None and occurred >= cutoff


def source_digest(payload):
    fields = {key: payload.get(key) for key in ('source_scope','message_id','thread_id',
        'occurred_at','direction','subject','excerpt','contact_emails')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def matching_draft(session, workspace_id, observation, occurred):
    """Only the original private MIME marker + saved recipient proves this draft."""
    marker = observation.get('crm_obligation')
    if (observation.get('direction') != 'outbound' or observation.get('sent_verified') is not True
            or not isinstance(marker, str) or not re.fullmatch(r'[a-f0-9]{64}@crm\.zelusottomayor\.com', marker)):
        return None
    root = os.environ.get('CRM_NOTE_DRAFT_JOURNAL_DIR', '')
    if not Path(root).is_absolute():
        return None
    for task in session.scalars(select(Task).where(Task.workspace_id == workspace_id,
            Task.task_type == 'email', Task.source_rule == 'head_of_sales_note',
            Task.created_at <= occurred)):
        expected = hashlib.sha256((str(workspace_id)+':'+str(task.id)).encode()).hexdigest()+'@crm.zelusottomayor.com'
        if marker != expected:
            continue
        try:
            saved = json.loads((Path(root)/str(workspace_id)/(str(task.id)+'.json')).read_text())
            recipient = saved['intent']['recipient'].casefold()
            if (saved.get('marker') == marker and saved.get('state') == 'verified'
                    and recipient in [v.casefold() for v in observation.get('contact_emails', [])]):
                return task
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    return None


def enqueue_email_update(session, lead, activity, payload, original_work_id, matched_task=None):
    if not active(activity.occurred_at):
        return None
    if matched_task is not None:
        session.refresh(matched_task, with_for_update=True)
        if (matched_task.lead_id == lead.id and matched_task.status == 'open'
                and matched_task.updated_at <= activity.occurred_at):
            matched_task.status = 'completed'
            matched_task.completed_at = activity.occurred_at
            matched_task.completion_activity_id = activity.id
            matched_task.updated_at = datetime.now(UTC)
            matched_task.version += 1
    return enqueue_work(session, workspace_id=lead.workspace_id, lead_id=lead.id,
        source_key='email-update:'+str(original_work_id), kind='email_update',
        payload=dict(payload, email_update_v1={'activity_id':str(activity.id),
            'source_work_id':str(original_work_id), 'source_digest':source_digest(payload)}))


def email_context(payload):
    marker = payload.get('email_update_v1') or {}
    return {'activity_id':marker.get('activity_id'), 'source_digest':source_digest(payload),
        'direction':payload.get('direction'), 'subject':payload.get('subject'),
        'text':current_text(payload.get('excerpt')), 'occurred_at':payload.get('occurred_at')}


def transition_target(current, requested, *, direction, quote, text, newer_stage=False):
    if requested is None or requested == current or newer_stage or is_terminal_stage(current):
        return current
    if requested != 'contacted' and (not quote or quote not in text):
        raise WorkConflict('Stage change requires a quote from the current email')
    if requested in ('won','lost','not_a_fit') and direction != 'inbound':
        raise WorkConflict('Customer outcome requires a received customer reply')
    if requested not in ('lost','not_a_fit') and stage_rank(requested) <= stage_rank(current):
        return current
    return requested


def apply_email_update(session, principal, work_id, body):
    work = locked_work(session, principal.workspace_id, work_id)
    request_hash = hashlib.sha256(body.model_dump_json(exclude={'lease_token'}).encode()).hexdigest()
    if (work.kind != 'email_update' or not work.lead_id or 'email_update_v1' not in work.payload):
        raise WorkConflict('Not an email update')
    if work.status == 'completed':
        if (work.result or {}).get('request_hash') != request_hash:
            raise WorkConflict('Email update replay changed')
        return serialize_work(work) | {'replayed':True}
    validate_lease(work, body.lease_token)
    payload = work.payload
    if source_digest(payload) != body.source_digest or payload['email_update_v1']['source_digest'] != body.source_digest:
        raise WorkConflict('Email source changed')
    lead = session.scalar(select(Lead).where(Lead.workspace_id == principal.workspace_id,
        Lead.id == work.lead_id).with_for_update(nowait=True))
    if lead is None or lead.version != body.expected_lead_version:
        raise WorkConflict('Lead changed; refresh context')
    activity = session.scalar(select(Activity).where(Activity.workspace_id == principal.workspace_id,
        Activity.id == UUID(payload['email_update_v1']['activity_id']), Activity.lead_id == lead.id,
        Activity.source_system == 'gmail', Activity.activity_type.in_(('email_sent','email_received'))).with_for_update())
    if activity is None or activity.direction != payload.get('direction'):
        raise WorkConflict('Email source unavailable')
    newer_stage = session.scalar(select(Activity.id).where(Activity.workspace_id == principal.workspace_id,
        Activity.lead_id == lead.id, Activity.activity_type == 'stage_change',
        Activity.occurred_at > activity.occurred_at).limit(1)) is not None
    # A later email update also wins over a delayed older message.
    for audit in session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == principal.workspace_id,
            AuditEvent.entity_id == lead.id, AuditEvent.action == 'agent.email_updated')):
        if datetime.fromisoformat(audit.details['occurred_at']) > activity.occurred_at:
            newer_stage = True
    previous = lead.stage
    target = transition_target(previous, body.target_stage, direction=activity.direction,
        quote=body.quote, text=current_text(payload.get('excerpt')), newer_stage=newer_stage)
    if target != previous:
        if requires_account(target, lead.highest_stage_rank) or lead.account_id:
            from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
            from src.crm.services.command_service import _ensure_account_for_transition
            uow = SqlAlchemyUnitOfWork(lambda:session).__enter__()
            from src.crm.services.command_service import CommandConflictError
            from src.crm.services.account_service import IdentityReviewRequired
            try:
                _ensure_account_for_transition(uow, lead, target)
            except (CommandConflictError, IdentityReviewRequired):
                raise WorkConflict('Account identity requires reconciliation') from None
        lead.stage = target
        lead.highest_stage_rank = highest_stage_rank(lead.highest_stage_rank, target)
        # Preserve phase history for Analysis without adding another visible note.
        session.add(Activity(id=uuid5(work.id, 'email-stage-change'),
            workspace_id=principal.workspace_id, account_id=lead.account_id, lead_id=lead.id,
            activity_type='stage_change', occurred_at=activity.occurred_at,
            title='Stage from email', from_stage=previous, to_stage=target,
            source_system='gmail', source_identity_id=activity.source_identity_id,
            outcome_code='email_stage_update', actor_type='agent', actor_id=principal.actor_id,
            semantic_fingerprint=request_hash))
        if is_terminal_stage(target):
            for task in session.scalars(select(Task).where(Task.workspace_id == principal.workspace_id,
                    Task.lead_id == lead.id, Task.status == 'open', Task.created_at <= activity.occurred_at,
                    Task.source_rule.in_(('head_of_sales_note','ordinary-followup/v1'))).with_for_update()):
                task.status = 'cancelled'; task.updated_at = datetime.now(UTC); task.version += 1
                # No Calendar writes: historical events remain untouched.
    lead.updated_at = datetime.now(UTC)
    details = {'work_id':str(work.id), 'activity_id':str(activity.id),
        'message_id':payload['message_id'], 'source_digest':body.source_digest,
        'occurred_at':activity.occurred_at.isoformat(), 'from_stage':previous, 'to_stage':target,
        'quote':body.quote, 'summary':body.summary, 'request_hash':request_hash}
    if len(json.dumps(details, ensure_ascii=False).encode()) > 3900:
        raise WorkConflict('Email evidence too large')
    session.add(AuditEvent(id=uuid5(work.id,'email-update-audit'), workspace_id=principal.workspace_id,
        command_id=uuid5(work.id,'email-update'), actor_id=principal.actor_id,
        action='agent.email_updated', entity_type='lead', entity_id=lead.id, details=details))
    session.flush()
    return finish_work(session, principal.workspace_id, work.id, body.lease_token,
        result={'summary':body.summary, 'request_hash':request_hash,
            'stage':target, 'previous_stage':previous, 'activity_id':str(activity.id),
            'evidence':[{'provider':'gmail','message_id':payload['message_id']}], 'next_action':None})


def email_history_summaries(session, workspace_id, lead_id, activity_ids):
    """Short read projection; immutable source activities remain original evidence."""
    ids = {str(value) for value in activity_ids}
    if not ids:
        return {}
    summaries = {}
    for work in session.scalars(select(AgentWork).where(
            AgentWork.workspace_id == workspace_id, AgentWork.lead_id == lead_id,
            AgentWork.kind == 'email_update',
            AgentWork.payload['email_update_v1']['activity_id'].astext.in_(ids))):
        activity_id = work.payload['email_update_v1']['activity_id']
        summaries[activity_id] = ('Email enviado. A aguardar resposta.'
            if work.payload.get('direction') == 'outbound' else 'Resposta recebida.')
    for audit in session.scalars(select(AuditEvent).where(
            AuditEvent.workspace_id == workspace_id, AuditEvent.entity_id == lead_id,
            AuditEvent.action == 'agent.email_updated',
            AuditEvent.details['activity_id'].astext.in_(ids))):
        summaries[audit.details['activity_id']] = audit.details['summary']
    return summaries
