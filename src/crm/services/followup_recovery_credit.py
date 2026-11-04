"""One audited attempt for the exact reviewed MIME incident; never a send grant."""
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field
from sqlalchemy import and_, select

from dashboard.app.schemas.followups import StrictBody, Identity, Hash, Version
from src.crm.persistence.models import AgentWork, AuditEvent, Lead, Task
from src.crm.services.agent_work_service import WorkConflict, lead_is_suppressed, locked_work, serialize_work
from src.crm.services.followup_context import digest
from src.crm.services.followup_review import save_ledger

from src.crm.services.followup_recovery_incident import INCIDENT
POLICY='followup-mime-recovery/2026-09-27'
ACTION='followup.recovery_credit.granted'


class RecoveryCreditBody(StrictBody):
    command_id: Identity
    expected_work_digest: Hash
    expected_lead_version: Version
    expected_task_version: Version
    context_hash: Hash
    source_key: str = Field(min_length=1,max_length=255)
    input_sha256: Hash
    draft_payload_sha256: Hash
    review_ref: str = Field(min_length=1,max_length=128)
    fix_commit: Literal['64e02cbaacdd3866b3eac369c9b0c430c2f98ab5']


def work_digest(work):
    return digest(serialize_work(work))


def binding(work, entry):
    return digest({'work_id':str(work.id),'lead_id':str(work.lead_id),'task_id':str(work.task_id),
                   'source_key':work.source_key,'input_sha256':entry['input_sha256'],
                   'draft_payload_sha256':entry['draft_payload_sha256']})


def response(work, replayed):
    credit=work.payload.get('followup_v1',{}).get('recovery_credit') or {}
    return {'work_id':str(work.id),'status':work.status,'attempt':work.attempts,
            'total_attempts':work.attempts + int(credit.get('status')=='consumed'),
            'credit_status':credit.get('status'),'policy':POLICY,'replayed':replayed}


def grant(session, principal, work_id, body, provider, *, now=None):
    from src.crm.services import followup_service as service
    now=now or datetime.now(UTC)
    work=locked_work(session,principal.workspace_id,work_id)
    request_digest=digest(body.model_dump(mode='json'))
    prior=session.scalar(select(AuditEvent).where(AuditEvent.workspace_id==principal.workspace_id,AuditEvent.command_id==body.command_id))
    if prior:
        if prior.action!=ACTION or prior.entity_id!=work.id or prior.actor_id!=principal.actor_id or prior.details.get('request_digest')!=request_digest:
            raise WorkConflict('recovery command conflict')
        return response(work,True)
    entry=INCIDENT['eligible_work'].get(str(work.id))
    state=deepcopy(work.payload.get('followup_v1') or {})
    if (not entry or binding(work,entry)!=entry['binding_sha256']
        or body.input_sha256!=entry['input_sha256'] or body.draft_payload_sha256!=entry['draft_payload_sha256']
        or body.source_key!=work.source_key or body.fix_commit!=INCIDENT['fix_commit']
        or work.kind!='followup_due' or work.status!='failed' or work.attempts!=3
        or work.error!='Worker lease expired' or work.result is not None or work.result_hash is not None
        or work.lease_token is not None or work.lease_until is not None
        or state.get('intent') is not None or state.get('decision') is not None
        or (state.get('review') or {}).get('requests') or (state.get('review') or {}).get('current_request_id')
        or state.get('recovery_credit') is not None or work_digest(work)!=body.expected_work_digest):
        raise WorkConflict('not eligible for incident recovery')
    # Company and exact task ownership are locked through the fresh snapshot.
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    _locked_company_lead(session,principal.workspace_id,work.lead_id)
    _locked_rows(session,select(Task).where(Task.workspace_id==work.workspace_id,Task.id==work.task_id))
    task=session.get(Task,work.task_id);lead=session.get(Lead,work.lead_id)
    if (not task or not lead or task.lead_id!=work.lead_id or task.status!='open'
        or task.task_type!='email' or task.source_rule!=service.POLICY_ID or task.version!=body.expected_task_version
        or state.get('task_version')!=task.version or work.payload.get('task_version')!=task.version
        or lead.version!=body.expected_lead_version or lead_is_suppressed(session,lead)):
        raise WorkConflict('recovery task or lead changed')
    obligation=state['obligation']
    snapshot=service.current_context(session,principal,work.lead_id,obligation['mailbox'],provider,thread_ids=[obligation['thread_id']])
    if snapshot['context_hash']!=body.context_hash or snapshot['source_versions']['lead_version']!=body.expected_lead_version:
        raise WorkConflict('recovery context changed')
    anchor=service.verified_anchor(snapshot,obligation['anchor_message_id'])
    assessment=service.assessment(snapshot,anchor,own_task_id=str(task.id))
    if assessment['disposition']!='eligible':
        raise WorkConflict('recovery source requires reconciliation')
    audit_id=uuid4()
    credit={'policy':POLICY,'command_id':str(body.command_id),'audit_id':str(audit_id),
            'status':'available','from_attempt':3,'max_attempt':4,'context_hash':body.context_hash,
            'binding_sha256':entry['binding_sha256'],'granted_at':now.isoformat()}
    details={'request_digest':request_digest,'review_ref':body.review_ref,'fix_commit':body.fix_commit,
             'input_sha256':body.input_sha256,'draft_payload_sha256':body.draft_payload_sha256,
             'old_work_digest':body.expected_work_digest,'previous_error':work.error,
             'previous_expected_lead_version':state['expected_lead_version'],'credit':credit}
    session.add(AuditEvent(id=audit_id,workspace_id=work.workspace_id,command_id=body.command_id,
                          actor_id=principal.actor_id,action=ACTION,entity_type='agent_work',entity_id=work.id,details=details))
    state['recovery_credit']=credit
    # New canonical context must receive new independent review. Never reuse a
    # decision, receipt, intent, model bytes or an old lease from the incident.
    state['expected_lead_version']=body.expected_lead_version
    state['assessment']=assessment
    save_ledger(work,state)
    work.status='queued';work.available_at=now;work.updated_at=now
    session.flush()
    return response(work,False)


def claim_filter():
    credit=AgentWork.payload['followup_v1']['recovery_credit']
    return and_(AgentWork.attempts==3,AgentWork.kind=='followup_due',
                AgentWork.id.in_([UUID(x) for x in INCIDENT['eligible_work']]),
                credit['policy'].astext==POLICY,credit['status'].astext=='available')


def consume_for_claim(session, work, now):
    """Called only under canonical claim row lock, without resetting normal attempts."""
    state=deepcopy(work.payload.get('followup_v1') or {});credit=state.get('recovery_credit') or {}
    entry=INCIDENT['eligible_work'].get(str(work.id))
    if (not entry or work.attempts!=3 or work.status!='queued' or credit.get('policy')!=POLICY
        or credit.get('status')!='available' or credit.get('from_attempt')!=3 or credit.get('max_attempt')!=4
        or binding(work,entry)!=entry['binding_sha256'] or credit.get('binding_sha256')!=entry['binding_sha256']
        or state.get('intent') is not None or state.get('decision') is not None):
        return False
    try:audit_id=UUID(credit['audit_id']);command_id=UUID(credit['command_id'])
    except (ValueError,KeyError,TypeError):return False
    receipt=session.scalar(select(AuditEvent).where(AuditEvent.workspace_id==work.workspace_id,AuditEvent.id==audit_id))
    if (not receipt or receipt.entity_id!=work.id or receipt.action!=ACTION or receipt.command_id!=command_id
        or receipt.details.get('credit')!=credit):return False
    credit=credit|{'status':'consumed','consumed_at':now.isoformat(),'used_attempt':4}
    state['recovery_credit']=credit;save_ledger(work,state)
    session.add(AuditEvent(workspace_id=work.workspace_id,command_id=uuid5(command_id,'claim'),actor_id=receipt.actor_id,
                          action='followup.recovery_credit.consumed',entity_type='agent_work',entity_id=work.id,
                          details={'grant_audit_id':str(audit_id),'attempt':4,'policy':POLICY}))
    return True
