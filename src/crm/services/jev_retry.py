"""One explicit, audited retry per factual work; original liability is retained."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from src.crm.persistence.models import Activity, AgentWork, AuditEvent, Contact
from src.crm.services.agent_work_service import WorkConflict
from src.crm.services.jev_note_policy import digest

ACTION = 'agent.jev_retry_authorized'
PREFIX = 'jev-note-retry/v1:'


class RetryBody(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    command_id: UUID
    jev_receipt_id: UUID
    expected_receipt_hash: str = Field(pattern='^[0-9a-f]{64}$')
    expected_lead_version: int = Field(ge=1, strict=True)
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    review_ref: str = Field(min_length=1, max_length=96, pattern=r'^\S(?:.*\S)?$')
    target_policy: Literal['crm-call-facts/v4']


@dataclass(frozen=True)
class RetryCommand:
    actor_id: UUID
    body: RetryBody


def _ids(workspace_id, work_id):
    key = PREFIX + str(work_id)
    return key, uuid5(workspace_id, key), uuid5(work_id, PREFIX)


def validate_retry_receipt(session, row):
    """Verify a complete fixed chain; used by dedup and the canonical writer."""
    result = row.result or {}
    if not row.source_key.startswith(PREFIX) and 'retry' not in result:
        return None
    from src.crm.services import jev_call_facts as facts
    from src.crm.services.typed_decisions import MODEL
    from src.crm.services.jev_note_runtime import RESERVATION_MICROEUR
    from src.crm.services.jev_note_worker import KIND
    try:
        work_id = UUID(result['work_id'])
        key, receipt_id, audit_id = _ids(row.workspace_id, work_id)
        audit = session.scalar(select(AuditEvent).where(AuditEvent.workspace_id == row.workspace_id,
            AuditEvent.id == audit_id, AuditEvent.action == ACTION, AuditEvent.entity_type == 'agent_work',
            AuditEvent.entity_id == work_id))
        if audit is None:
            raise ValueError
        details = audit.details
        original_id = UUID(details['original_receipt_id'])
        original = session.scalar(select(AgentWork).where(AgentWork.workspace_id == row.workspace_id,
            AgentWork.id == original_id, AgentWork.kind == KIND))
        old = original.result if original is not None else {}
        original_key = 'jev-note-work:' + digest({'target': result['target'], 'work_id': str(work_id)})
        if (row.kind != KIND or row.source_key != key or row.id != receipt_id
                or result.get('receipt_id') != str(row.id) or original is None
                or original.id != uuid5(row.workspace_id, original_key) or original.source_key != original_key
                or original.status != 'completed' or old.get('status') != 'failed'
                or original.result_hash != details['original_receipt_hash']
                or original.result_hash != digest(old) or 'retry' in old
                or result.get('retry') != {'original_receipt_id': str(original.id), 'audit_id': str(audit.id)}
                or details['retry_receipt_id'] != str(row.id)
                or details['command']['jev_receipt_id'] != str(original.id)
                or details['command']['command_id'] != str(audit.command_id)
                or details['command']['expected_receipt_hash'] != original.result_hash
                or details['command']['source_digest'] != old.get('source_digest')
                or details['command']['target_policy'] != facts.VERSION
                or original.lead_id != row.lead_id
                or original.payload != row.payload
                or row.payload.get('flags') != facts.FLAGS
                or any(old.get(k) != result.get(k) for k in ('work_id','target','decision_id',
                    'snapshot_digest','source_digest','version','model','event_id','simulated'))
                or old.get('version') != facts.VERSION or old.get('model') != MODEL
                or old.get('simulated') is not False
                or old.get('reserved_microeur') != RESERVATION_MICROEUR
                or result.get('reserved_microeur') != RESERVATION_MICROEUR):
            raise ValueError
        if result.get('status') != 'pending' and row.result_hash != digest(result):
            raise ValueError
        return original
    except (KeyError, ValueError, TypeError, AttributeError):
        raise WorkConflict('Retry audit chain invalid') from None


def replay_row(session, priors):
    retries = [row for row in priors if row.source_key.startswith(PREFIX) or 'retry' in (row.result or {})]
    if len(priors) != 2 or len(retries) != 1:
        raise WorkConflict('Ambiguous operational receipts')
    retry = retries[0]
    original = validate_retry_receipt(session, retry)
    if {row.id for row in priors} != {original.id, retry.id}:
        raise WorkConflict('Ambiguous operational receipts')
    return retry


def claim_retry(session, request, runtime, work, priors, receipt, owner, deadline_seconds, budgets):
    """Called inside the existing intent/budget transaction, before any HTTP."""
    from src.crm.services import jev_call_facts as facts
    from src.crm.services.typed_decisions import MODEL
    from src.crm.services.jev_note_worker import KIND, _build_request, _expire, load_snapshot
    from src.crm.services.jev_note_runtime import RESERVATION_MICROEUR
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    command = runtime.retry
    if (type(command) is not RetryCommand or type(command.body) is not RetryBody
            or type(command.actor_id) is not UUID or request['version'] != facts.VERSION
            or facts.VERSION != 'crm-call-facts/v4' or command.body.target_policy != facts.VERSION
            or request['model'] != MODEL or request['flags'] != facts.FLAGS):
        raise WorkConflict('Explicit current factual retry required')
    body = command.body
    key, receipt_id, audit_id = _ids(work.workspace_id, work.id)
    prior_audit = session.scalar(select(AuditEvent).where(AuditEvent.workspace_id == work.workspace_id,
        or_(AuditEvent.id == audit_id, AuditEvent.command_id == body.command_id)))
    if prior_audit is not None:
        if (prior_audit.id != audit_id or prior_audit.actor_id != command.actor_id
                or prior_audit.command_id != body.command_id
                or prior_audit.details.get('command') != body.model_dump(mode='json')):
            raise WorkConflict('Retry command already used')
        row = replay_row(session, priors)
        return None, _expire(session, row)
    if len(priors) != 1:
        raise WorkConflict('Exactly one original receipt required')
    original = priors[0]
    old = original.result or {}
    original_key = 'jev-note-work:' + digest({'target':runtime.target,'work_id':str(work.id)})
    if (original.id != body.jev_receipt_id or original.source_key != original_key
            or original.id != uuid5(work.workspace_id, original_key)
            or original.status != 'completed' or old.get('status') != 'failed'
            or original.result_hash != body.expected_receipt_hash or original.result_hash != digest(old)
            or old.get('simulated') is not False or 'retry' in old
            or old.get('work_id') != str(work.id) or old.get('target') != runtime.target
            or original.lead_id != work.lead_id or not runtime.accepts_ref(old.get('budget_ref'))
            or old.get('reserved_microeur') != RESERVATION_MICROEUR
            or old.get('version') != facts.VERSION or old.get('model') != MODEL
            or original.payload.get('flags') != facts.FLAGS
            or original.payload.get('event_id') != work.payload.get('activity_id')
            or original.payload.get('source_digest') != body.source_digest
            or original.payload.get('source_digest') != work.payload.get('source_digest')):
        raise WorkConflict('Immutable failed v4 receipt required')
    # Fence human/source changes across admission. No lock is held during HTTP;
    # the unchanged canonical writer revalidates everything before promotion.
    _locked_rows(session, select(AgentWork).where(AgentWork.workspace_id == work.workspace_id, AgentWork.id == work.id))
    lead, _ = _locked_company_lead(session, work.workspace_id, work.lead_id)
    if lead.version != body.expected_lead_version:
        raise WorkConflict('Lead changed')
    if lead.contact_id:
        _locked_rows(session, select(Contact).where(Contact.workspace_id == work.workspace_id, Contact.id == lead.contact_id))
    scope = or_(Activity.lead_id == lead.id, Activity.account_id == lead.account_id) if lead.account_id else Activity.lead_id == lead.id
    _locked_rows(session, select(Activity).where(Activity.workspace_id == work.workspace_id, scope))
    runtime.validate_source(session, work)
    fresh = _build_request(load_snapshot(session, work.workspace_id, lead.id, work.payload), facts.FLAGS)
    if (fresh != request or fresh['decision_id'] != old.get('decision_id')
            or fresh['snapshot_digest'] != old.get('snapshot_digest')):
        raise WorkConflict('Original inference source changed')
    budget = next((row for row in budgets if row.result['reserved_microeur'] + RESERVATION_MICROEUR <= row.payload['cap_microeur']), None)
    if budget is None:
        raise WorkConflict('Budget exhausted')
    receipt.update(receipt_id=str(receipt_id), target=runtime.target, budget_ref=budget.payload['budget_ref'],
        reserved_microeur=RESERVATION_MICROEUR, retry={'original_receipt_id':str(original.id),'audit_id':str(audit_id)})
    now = session.scalar(select(func.now()))
    session.add(AgentWork(id=receipt_id, workspace_id=work.workspace_id, lead_id=work.lead_id,
        source_key=key, kind=KIND, status='waiting', worker_id=owner, attempts=1,
        available_at=now + timedelta(seconds=deadline_seconds), payload=deepcopy(original.payload), result=deepcopy(receipt)))
    session.add(AuditEvent(id=audit_id, workspace_id=work.workspace_id, command_id=body.command_id,
        actor_id=command.actor_id, action=ACTION, entity_type='agent_work', entity_id=work.id,
        details={'command':body.model_dump(mode='json'),'original_receipt_id':str(original.id),
            'original_receipt_hash':original.result_hash,'retry_receipt_id':str(receipt_id),
            'additional_reserved_microeur':RESERVATION_MICROEUR,'original_liability_retained':True,
            'provider_exactly_once_claimed':False}))
    budget.result = {'reserved_microeur':budget.result['reserved_microeur'] + RESERVATION_MICROEUR}
    session.flush()
    return owner, receipt
