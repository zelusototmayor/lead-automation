"""Finite, immutable two-case continuation. No regrant or historical replay."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5
import json
from sqlalchemy import select
from src.crm.persistence.models import AuditEvent, Task
from src.crm.services.agent_work_service import WorkConflict, serialize_work
from src.crm.services.followup_context import digest, dto
from src.crm.services.followup_review import save_ledger, timestamp, FollowupError

POLICY = 'expired-followup-handoff/v1'
ACTION = 'followup.expired_handoff_resumed'
BINDINGS = json.loads(Path(__file__).with_name('followup_expired_bindings.json').read_text())


def require(ok):
    if not ok:
        raise WorkConflict('Expired followup continuation evidence changed')


def historical_evidence(session, principal, work, body, now):
    """Exact private bytes, audited grant/consume and immutable failure witness."""
    c = BINDINGS.get(str(work.id)); require(c is not None)
    state = work.payload.get('followup_v1') or {}; grant = state.get('preparation_recovery') or {}
    require(str(work.workspace_id) == str(principal.workspace_id) == c['workspace_id']
        and str(principal.actor_id) == c['actor_id'] and work.worker_id == body.worker_id == c['worker_id']
        and work.kind == 'followup_due' and work.status == 'failed' and work.attempts == 1
        and work.updated_at == body.expected_updated_at == timestamp(c['updated_at'])
        and work.available_at <= now and work.lease_token is None and work.lease_until is None
        and work.result is None and work.result_hash is None and state.get('intent') is None
        and digest(work.payload) == c['payload_sha256'] and digest(grant) == c['grant_sha256']
        and body.expected_work_digest == digest(dto(work))
        and str(body.expected_expired_review_request_id) == c['request_id'])
    for binding in c['audits']:
        event = session.get(AuditEvent, UUID(binding['id']))
        require(event is not None and digest(dto(event)) == binding['sha256'])
    request = state['review']['requests'][-1]; p = request['package']
    require(state['review']['current_request_id'] == c['request_id']
        and request['request_hash'] == digest(p) == c['request_hash']
        and p['work_id'] == str(work.id) and p['workspace_id'] == str(work.workspace_id)
        and 'purpose' not in p and p['obligation_key'] == work.source_key
        and timestamp(p['created_at']) >= timestamp(grant['consumed_at'])
        and timestamp(p['created_at']) < timestamp(p['expires_at']) <= now)
    # Hash pins the entire decision, receipt, requests and all old findings. A new
    # rejection, different decision or intent never qualifies under this contract.
    return c, deepcopy(state)


def fresh_context(session, principal, work, body, now, provider=None):
    from src.crm.services import followup_service as service
    from src.crm.services.followup_missing_effect import _lock_snapshot
    if provider is None:
        import os
        from src.crm.connectors.followup_email import GmailProvider
        credentials = os.environ.get('GOOGLE_GMAIL_CREDENTIALS_FILE')
        require(bool(credentials))
        provider = GmailProvider(credentials_file=credentials)
    _context, snapshot, anchor, task = service.payload_context(session, principal, work, provider, now=now)
    s = work.payload['followup_v1']; o = s['obligation']
    require(snapshot['context_hash'] == body.expired_followup_context_hash
        and all(snapshot['coverage'].get(k) is True for k in ('complete', 'crm_complete', 'gmail_complete', 'direct_drafts_complete', 'related_threads_complete'))
        and task.workspace_id == work.workspace_id and task.lead_id == work.lead_id
        and task.task_type == 'email' and work.task_id == service.task_id(work.workspace_id, work.source_key)
        and task.version == work.payload['task_version'] == s['task_version']
        and o['key'] == work.source_key == service.obligation_key(work.workspace_id, o['mailbox'], work.lead_id, o['thread_id'], o['fu_stage'], o['anchor_message_id'])
        and anchor['thread_id'] == o['thread_id']
        and max(task.due_at, service.due_at(timestamp(anchor['sent_at']), o['fu_stage'])) <= now)
    _lock_snapshot(session, work.workspace_id, snapshot)
    return snapshot


def recover(session, principal, work, body, *, now=None, provider=None):
    """Caller holds locked_work and transaction. At most one successor per grant."""
    from src.crm.services.followup_preparation_recovery import mailbox_readiness
    from src.crm.services.followup_service import mailbox_scope
    now = now or datetime.now(UTC)
    require(body.expected_work_digest is not None and body.expired_followup_context_hash is not None
        and body.expected_expired_review_request_id is not None)
    require(not any(getattr(body, k) is not None for k in (
        'expected_context_read_failure','context_failure_observed_at','context_failure_evidence_sha256',
        'expected_prepare_read_failure','prepare_failure_observed_at','prepare_failure_evidence_sha256',
        'false_reject_certificate_sha256','false_reject_context_hash')))
    c = BINDINGS.get(str(work.id)); require(c is not None)
    require(str(principal.workspace_id) == str(work.workspace_id) == c['workspace_id']
        and str(principal.actor_id) == c['actor_id'] and body.worker_id == work.worker_id == c['worker_id'])
    mailbox_scope(principal, work.payload['followup_v1']['obligation']['mailbox'])
    command = uuid5(UUID(c['grant_command_id']), POLICY)
    request_digest = digest(body.model_dump(mode='json'))
    previous = session.get(AuditEvent, command)
    if previous:
        fence = work.payload['followup_v1'].get('expired_handoff') or {}
        require(previous.action == ACTION and previous.workspace_id == work.workspace_id
            and previous.entity_id == work.id and previous.actor_id == principal.actor_id
            and previous.details.get('request_digest') == request_digest
            and fence.get('command_id') == str(command)
            and fence.get('request_digest') == request_digest)
        active = (work.status == 'running' and work.lease_until is not None and work.lease_until > now
            and work.lease_token is not None and str(work.lease_token) == fence.get('lease_token'))
        return {'items': [serialize_work(work, include_lease=True)] if active else [], 'status':work.status, 'replayed':True}
    c, state = historical_evidence(session, principal, work, body, now)
    ready = mailbox_readiness(session, work, now)
    if ready['status'] != 'ready':
        return {'items':[], 'status':'deferred', 'readiness':ready}
    snapshot = fresh_context(session, principal, work, body, now, provider)
    lease = uuid4()
    fence = {'policy':POLICY, 'command_id':str(command), 'audit_id':str(command),
        'request_digest':request_digest, 'original_updated_at':body.expected_updated_at.isoformat(),
        'original_work_digest':body.expected_work_digest, 'fresh_context_hash':snapshot['context_hash'],
        'binding_sha256':digest(c), 'protected_request_ids':[r['package']['request_id'] for r in state['review']['requests']],
        'historical_decision':deepcopy(state.get('decision')), 'failure_witness':c['failure'],
        'lease_token':str(lease), 'fresh_review_required':True, 'maximum_continuations':1}
    state['expired_handoff'] = fence
    state['decision'] = None  # Exact prior bytes are retained above and in the immutable request.
    save_ledger(work, state)
    session.add(AuditEvent(id=command, workspace_id=work.workspace_id, command_id=command,
        actor_id=principal.actor_id, action=ACTION, entity_type='agent_work', entity_id=work.id,
        details={'request_digest':request_digest, 'fence':fence, 'normal_attempts_preserved':work.attempts}))
    work.status, work.lease_token, work.lease_until, work.updated_at = 'running', lease, now+timedelta(seconds=600), now
    session.flush()
    return {'items':[serialize_work(work,include_lease=True)], 'status':'running', 'replayed':False}


def review_evidence(state):
    f = state.get('expired_handoff')
    if not f:
        return None
    return {k:deepcopy(v) for k,v in f.items() if k != 'lease_token'}


def require_successor(state, item, decision, lease_token):
    f = state.get('expired_handoff')
    if not f:
        return
    require(bool(item) and bool(decision)
        and decision['review_request_id'] not in f['protected_request_ids']
        and str(lease_token) == f['lease_token'] == item['lease_token']
        and item['package'].get('preparation_recovery',{}).get('expired_handoff') == review_evidence(state))
