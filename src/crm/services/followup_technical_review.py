"""Finite technical receipt succession within the existing preparation protocol.

No historical verdict is overridden. The two source-bound certificates admit
one fresh preparation; any successor receipt (including process_error) is final.
"""
import json
from pathlib import Path
from sqlalchemy import select
from src.crm.persistence.models import AuditEvent, Lead, Task
from src.crm.services.agent_work_service import WorkConflict
from src.crm.services.followup_context import digest

REASON = 'technical_review_failure'
BINDINGS = json.loads(Path(__file__).with_name('followup_technical_review_bindings.json').read_text())


def validate(session, work, state, body):
    if body.reason != REASON:
        return None
    binding = BINDINGS.get(str(work.id))
    review = state.get('review') or {}
    requests = review.get('requests') or []
    item = requests[-1] if requests else {}
    receipt = item.get('receipt') or {}
    package = item.get('package') or {}
    if (not binding or any(str(getattr(work, key)) != binding[key]
            for key in ('workspace_id', 'lead_id', 'task_id', 'source_key'))
            or work.kind != 'followup_due' or work.attempts != 1
            or state.get('preparation_recovery') is not None
            or state.get('recovery_credit') is not None
            or state.get('expired_handoff') is not None
            or state.get('intent') is not None or state.get('decision') is not None
            or any((r.get('receipt') or {}).get('verdict', {}).get('decision') in ('REJECT', 'JOSE_REQUIRED')
                   for r in requests if (r.get('receipt') or {}).get('verdict'))
            or work.error != 'followup_handoff:review_rejected'
            or review.get('current_request_id') != binding['request_id']
            or package.get('request_id') != binding['request_id']
            or package.get('work_id') != str(work.id)
            or package.get('workspace_id') != str(work.workspace_id)
            or package.get('obligation_key') != work.source_key
            or item.get('request_hash') != binding['request_hash']
            or digest(package) != binding['request_hash']
            or digest(receipt) != binding['receipt_sha256']
            or str(body.expected_review_request_id) != binding['request_id']
            or body.expected_review_request_hash != binding['request_hash']
            or body.expected_review_receipt_hash != binding['receipt_sha256']
            or item.get('status') != 'rejected' or not item.get('consumed_at')
            or receipt.get('runner_outcome') != 'process_error'
            or receipt.get('verdict') is not None
            or receipt.get('authenticated_actor_id') != package.get('reviewer_actor_id')
            or not body.evidence_text.strip()):
        raise WorkConflict('technical review evidence changed or not admissible')
    events = list(session.scalars(select(AuditEvent).where(
        AuditEvent.workspace_id == work.workspace_id, AuditEvent.entity_id == work.id,
        AuditEvent.action == 'followup.review_consumed')))
    matches = [event for event in events if event.details.get('request_id') == binding['request_id']]
    if (len(matches) != 1 or str(matches[0].actor_id) != receipt['authenticated_actor_id']
            or matches[0].details.get('receipt_id') != receipt.get('id')
            or matches[0].details.get('reason_code') != 'review_process_error'):
        raise WorkConflict('technical review audit unavailable')
    return dict(binding)


def same_source(work, snapshot):
    """A technical retry cannot reconcile new human/provider source evidence."""
    binding = BINDINGS.get(str(work.id)) or {}
    requests = (work.payload['followup_v1'].get('review') or {}).get('requests') or []
    originals = [r for r in requests if r.get('package', {}).get('request_id') == binding.get('request_id')]
    if len(originals) != 1:
        return False
    context = originals[0]['package'].get('context') or {}
    return all(key in context and digest(snapshot[key]) == digest(context[key])
               for key in ('crm', 'provider'))


def fresh_claim(session, work, value, now, principal, provider_factory):
    """Inside the existing exact claim transaction; no separate consumer."""
    from src.crm.services import followup_service as service
    from src.crm.services.followup_review import FollowupError
    binding = value.get('technical_review_successor')
    if not binding:
        return True
    if (not principal or not provider_factory or principal.workspace_id != work.workspace_id
            or str(principal.actor_id) != value.get('actor_id')
            or BINDINGS.get(str(work.id)) != binding):
        return False
    service.mailbox_scope(principal, work.payload['followup_v1']['obligation']['mailbox'])
    state = work.payload['followup_v1']
    task, lead = session.get(Task, work.task_id), session.get(Lead, work.lead_id)
    if (not task or not lead or task.workspace_id != work.workspace_id
            or task.lead_id != work.lead_id or task.status != 'open'
            or task.task_type != 'email' or task.source_rule != service.POLICY_ID
            or task.due_at > now or task.version != state.get('task_version')
            or task.version != work.payload.get('task_version')
            or lead.version != state.get('expected_lead_version')
            or state.get('intent') is not None or state.get('recovery_credit') is not None):
        return False
    obligation = state['obligation']
    try:
        snapshot = service.current_context(session, principal, work.lead_id,
            obligation['mailbox'], provider_factory(), thread_ids=[obligation['thread_id']])
        if snapshot['context_hash'] != value['context_hash']:
            return False
        anchor = service.verified_anchor(snapshot, obligation['anchor_message_id'])
        return (anchor['thread_id'] == obligation['thread_id'] and
            service.assessment(snapshot, anchor, own_task_id=str(task.id))['disposition'] == 'eligible')
    except (FollowupError, ValueError):
        return False
