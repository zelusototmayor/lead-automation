"""Operator-reviewed withdrawal of one contradicted derived attendance field.

No inference, receipt mutation, source edit, work completion or business effect.
"""
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.crm.persistence.models import AuditEvent
from src.crm.services.agent_work_service import WorkConflict, locked_work, lead_is_suppressed
from src.crm.services.jev_note_policy import digest
from src.crm.services.note_fact_projection import WITHDRAWAL_ACTION


class WithdrawFactBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: UUID
    expected_lead_version: int = Field(ge=1, strict=True)
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    target_audit_id: UUID
    target_audit_payload_digest: str = Field(pattern='^[0-9a-f]{64}$')
    supporting_audit_id: UUID
    supporting_audit_payload_digest: str = Field(pattern='^[0-9a-f]{64}$')
    field: Literal['answer_kind']
    withdrawn_value: Literal['no_answer']
    reason_code: Literal['semantic_model_error']
    quote: str = Field(min_length=1, max_length=400)
    review_ref: str = Field(min_length=1, max_length=120)


def withdraw_fact(session, principal, work_id, body):
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    from src.crm.services.note_source_service import note_context
    from src.crm.persistence.models import Activity
    row = locked_work(session, principal.workspace_id, work_id)
    lead, _ = _locked_company_lead(session, principal.workspace_id, row.lead_id)
    if (row.kind != 'call_followup' or lead.version != body.expected_lead_version
            or lead_is_suppressed(session, lead)):
        raise WorkConflict('Source or lead changed')
    _locked_rows(session, select(Activity).where(Activity.workspace_id == principal.workspace_id,
                                                Activity.lead_id == lead.id))
    source = note_context(session, principal.workspace_id, lead.id, row.payload)
    if (source.get('status') != 'current' or source.get('activity_type') != 'call'
            or source.get('source_digest') != body.source_digest
            or not body.quote.strip() or body.quote not in (source.get('summary') or '')
            or not body.review_ref.strip()):
        raise WorkConflict('Current cited source required')
    audits = _locked_rows(session, select(AuditEvent).where(
        AuditEvent.workspace_id == principal.workspace_id,
        AuditEvent.id.in_([body.target_audit_id, body.supporting_audit_id])))
    by_id = {item.id: item for item in audits}
    target, support = by_id.get(body.target_audit_id), by_id.get(body.supporting_audit_id)
    if (target is None or support is None or target.id == support.id
            or any(item.action != 'agent.note_interpreted' or item.entity_type != 'activity'
                   or str(item.entity_id) != source['activity_id']
                   or (item.details or {}).get('source_digest') != body.source_digest
                   or (item.details or {}).get('work_id') != str(work_id) for item in (target, support))
            or digest(target.details) != body.target_audit_payload_digest
            or digest(support.details) != body.supporting_audit_payload_digest
            or (target.details.get('facts') or {}).get('answer_kind') != 'no_answer'
            or (support.details.get('facts') or {}).get('answer_kind') != 'human_counterparty'
            or not (target.details.get('jev') or {}).get('receipt_id')):
        raise WorkConflict('Exact conflicting interpreted audits required')
    # The receipt remains immutable and current, with its original model answer.
    from src.crm.services.jev_note_writer_bridge import bind_receipt
    bind_receipt(session, principal.workspace_id, row, lead,
                 UUID(target.details['jev']['receipt_id']), factual=True)
    request = body.model_dump(mode='json')
    request_hash = digest(request)
    identity = uuid5(body.command_id, 'note-fact-withdrawal:' + str(principal.workspace_id))
    previous = session.scalar(select(AuditEvent).where(AuditEvent.workspace_id == principal.workspace_id,
                                                     AuditEvent.command_id == body.command_id))
    if previous is not None:
        if (previous.id != identity or previous.action != WITHDRAWAL_ACTION
                or previous.actor_id != principal.actor_id or str(previous.entity_id) != source['activity_id']
                or (previous.details or {}).get('request_hash') != request_hash):
            raise WorkConflict('Adjudication command already used')
        return dict(previous.details['result'], replayed=True)
    if session.scalar(select(AuditEvent.id).where(AuditEvent.workspace_id == principal.workspace_id,
            AuditEvent.entity_id == UUID(source['activity_id']), AuditEvent.action == WITHDRAWAL_ACTION,
            AuditEvent.details['target_audit_id'].astext == str(target.id),
            AuditEvent.details['field'].astext == body.field).limit(1)):
        raise WorkConflict('Field already reviewed; replay its existing command')
    result = {'status': 'reviewed_adjudication', 'audit_id': str(identity),
        'target_audit_id': str(target.id), 'withdrawn_field': body.field,
        'reason_code': body.reason_code, 'source_work_unchanged': True,
        'inference_performed': False, 'fact_authority': False}
    session.add(AuditEvent(id=identity, workspace_id=principal.workspace_id,
        command_id=body.command_id, actor_id=principal.actor_id, action=WITHDRAWAL_ACTION,
        entity_type='activity', entity_id=UUID(source['activity_id']), details={
            **request, 'version': 'note-fact-withdrawal/v1', 'status': 'reviewed_adjudication',
            'work_id': str(work_id), 'request_hash': request_hash, 'result': result}))
    session.flush()
    return dict(result, replayed=False)
