"""Explicit one-shot preparation allowance, not send or commercial authority.

Old incident credits, attempts and private review history remain unchanged.
Only exact-ID canonical claims may consume this separately audited allowance.
"""
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field
from sqlalchemy import and_, or_, select

from dashboard.app.schemas.followups import StrictBody, Identity, Hash, Version
from src.crm.persistence.models import AgentWork, AuditEvent, Lead, Task
from src.crm.services.agent_work_service import WorkConflict, locked_work
from src.crm.services.followup_context import crm_snapshot, digest, dto
from src.crm.services.followup_review import FollowupError, ledger_name, save_ledger

POLICY = 'followup-preparation-recovery/v1'
ACTION = 'followup.preparation_recovery.granted'

# Fixed diagnostic codes; no exception text or context data is exposed.
PREPARATION_SOURCE_CONFLICT_CODES = {'closed_or_suppressed': 'preparation_closed_or_suppressed', 'contact_inactive': 'preparation_contact_inactive', 'source_suppressed': 'preparation_source_suppressed', 'explicit_company_exclusion': 'preparation_explicit_company_exclusion', 'nonordinary_stage': 'preparation_nonordinary_stage', 'meeting_or_proposal': 'preparation_meeting_or_proposal', 'existing_direct_draft': 'preparation_existing_direct_draft', 'newer_message_requires_reconciliation': 'preparation_newer_message_requires_reconciliation', 'substantive_human_reply': 'preparation_substantive_human_reply', 'automatic_reply_requires_reconciliation': 'preparation_automatic_reply_requires_reconciliation', 'human_or_other_obligation': 'preparation_human_or_other_obligation'}
ERRORS = frozenset({
    'followup_handoff:review_or_execution_not_finalized',
    'followup_handoff:execution_error:ValueError',
    'followup_handoff:execution_error:AdapterError',
    'Worker lease expired',
})


RUNTIME_REPAIR_INCIDENT = 'runtime_repair_20261006'
RUNTIME_REPAIR_WORK_IDS = frozenset({
    'ffb6ab36-448e-5857-b3be-1ddf8d10a517',  # Bootstrap failed before model/review.
    '8dbc4341-df17-5f30-b81b-501061605756',  # Review package parser failed before verdict.
})


OWNER_REASSESSMENT_INCIDENT = 'owner_reassessment_20261006'
# Pin the exact accepted reviews inspected for the owner's 6 October request.
# Joel was APPROVED but blocked locally before execution; the other case asked
# José about backlog authority. Neither record is a generic review override.
OWNER_REASSESSMENTS = {
    'ffb6ab36-448e-5857-b3be-1ddf8d10a517': {
        'request_id': '542fe099-125c-45e1-8cfc-835169eda2e5',
        'request_hash': '1cc06a2c55c40a73d26d84000832752e105e0e7e4bca90d261740f0371f5fd00',
        'receipt_hash': '22184f987af1cf44ca9d0c2d7029870a492af0590174f63e11068b64e7c5e3f1',
        'decision': 'APPROVE',
    },
    '0d1653f9-e099-5c22-8d51-5907b769070a': {
        'request_id': '5ec3d534-ec5b-4b16-9b84-2ab78e3c953c',
        'request_hash': 'bb320de4921af56a54ee28bf0c37e11a29283e71c1a217f0d48446a4548a33a3',
        'receipt_hash': 'c63571790a2f007c69f1d85d0f3aaa42be80024d37193a1c47031dfdbf0e8119',
        'decision': 'JOSE_REQUIRED',
    },
}


class PreparationRecoveryBody(StrictBody):
    command_id: Identity
    expected_work_digest: Hash
    source_key: str = Field(min_length=1, max_length=255)
    expected_lead_version: Version
    expected_task_version: Version | None
    context_hash: Hash
    evidence_ref: str = Field(min_length=1, max_length=512)
    evidence_text: str = Field(default='', max_length=6000)
    reason: Literal['saved_pre_send_failure', 'runtime_repair_20261006', 'owner_reassessment_20261006', 'technical_review_failure', 'aborted_before_send', 'owner_reassessment']
    expected_review_request_id: Identity | None = None
    expected_review_request_hash: Hash | None = None
    expected_review_receipt_hash: Hash | None = None
    expected_preparation_recovery_hash: Hash | None = None


def identity(work):
    return {'work_id': str(work.id), 'lead_id': str(work.lead_id),
            'task_id': str(work.task_id) if work.task_id else None,
            'source_key': work.source_key, 'kind': work.kind}


def work_digest(work):
    # Includes PRIVATE requests/decisions; never hash the redacted projection.
    return digest(dto(work))


def state_and_mailbox(work):
    from src.crm.services.followup_review import FollowupError
    names = [n for n in ('followup_v1', 'followup_anchor_v1') if n in work.payload]
    if len(names) != 1:
        raise WorkConflict('not canonical preparation work')
    name = names[0]
    state = work.payload[name]
    mailbox = (state.get('obligation') or {}).get('mailbox') if name == 'followup_v1' else state.get('mailbox')
    if not mailbox:
        raise FollowupError('mailbox_forbidden', 403)
    return name, state, mailbox


def mailbox_readiness(session, work, now):
    """Reuse the execution authority; no reservation, grant or ledger write.

    The transaction-scoped mailbox fence also closes the read->claim race when
    used immediately before consuming the finite allowance, work->mailbox->company.
    Execution still rechecks this fence after mandatory fresh MIME review.
    """
    from src.crm.services.followup_service import reserve_slot, SpacingPending
    from src.crm.services.followup_review import FollowupError
    if ledger_name(work) != 'followup_v1':
        return {'status': 'not_applicable'}
    try:
        reserve_slot(session, work, now)
    except SpacingPending as exc:
        return {'status': 'deferred', 'reason_code': exc.code,
                'not_before': exc.not_before.isoformat()}
    except FollowupError as exc:
        return {'status': 'deferred', 'reason_code': exc.code}
    return {'status': 'ready'}


def projection(session, principal, work_id):
    from src.crm.services.followup_service import mailbox_scope
    work = locked_work(session, principal.workspace_id, work_id)
    name, state, mailbox = state_and_mailbox(work)
    mailbox_scope(principal, mailbox)
    latest = ((state.get('review') or {}).get('requests') or [{}])[-1]
    peer_preflight = ({'false_reject_recovery_ready': false_reject_recovery_preflight(session, principal, work)}
                      if str(work.id) in AMERICOLD_PEER_FALSE_REJECT_CERTIFICATES else {})
    return identity(work) | peer_preflight | {'work_digest': work_digest(work), 'status': work.status,
                            'current_review_request_id': latest.get('package', {}).get('request_id'),
                            'current_review_request_hash': latest.get('request_hash'),
                            'current_review_receipt_hash': digest(latest.get('receipt')) if latest else None,
                            'preparation_recovery_hash': digest(state.get('preparation_recovery')),
                            'attempt': work.attempts, 'task_version': state.get('task_version'),
                            'mailbox_readiness': mailbox_readiness(session, work, datetime.now(UTC))}


def stable_work(work):
    payload = deepcopy(work.payload)
    payload[ledger_name(work)].pop('preparation_recovery', None)
    return digest(identity(work) | {'payload': payload, 'attempts': work.attempts,
                                   'error': work.error, 'result': work.result,
                                   'result_hash': work.result_hash})


def response(work, receipt, replayed):
    return {'work_id': str(work.id), 'receipt': receipt, 'replayed': replayed,
            'current_work_status': work.status}


def technical_repair(work, state, body):
    """One exact incident repair; never a new commercial or review permission."""
    if body.reason != RUNTIME_REPAIR_INCIDENT:
        return False
    previous = state.get('preparation_recovery') or {}
    review = state.get('review') or {}
    if (str(work.id) not in RUNTIME_REPAIR_WORK_IDS
            or previous.get('policy') != POLICY or previous.get('status') != 'consumed'
            or previous.get('technical_repair_incident') is not None
            or not body.evidence_text.strip() or state.get('decision') is not None
            or any(g.get('technical_repair_incident') == RUNTIME_REPAIR_INCIDENT
                   for g in review.get('preparation_recovery_history', []))):
        raise WorkConflict('not eligible for fixed runtime repair')
    # Earlier grants already carried old findings into independent fresh review.
    # This incident happened BEFORE any new verdict. A subsequent receipt or
    # rejection is substantive work, and cannot be relabelled a runtime fault.
    previous_ids = set(previous.get('prior_request_ids') or [])
    for request in review.get('requests', []):
        if request['package']['request_id'] in previous_ids:
            continue
        if (request.get('receipt') is not None
                or request.get('status') not in {'pending_review', 'invalidated', 'expired'}):
            raise WorkConflict('runtime repair cannot override a reviewer verdict')
    return True


def owner_reassessment(work, state, body):
    """One new review of two fixed owner cases, never reuse or force a verdict."""
    if body.reason != OWNER_REASSESSMENT_INCIDENT:
        return False
    binding = OWNER_REASSESSMENTS.get(str(work.id))
    previous = state.get('preparation_recovery') or {}
    review = state.get('review') or {}
    requests = review.get('requests') or []
    latest = requests[-1] if requests else {}
    receipt = latest.get('receipt') or {}
    if (not binding or previous.get('policy') != POLICY or previous.get('status') != 'consumed'
            or previous.get('owner_reassessment_incident') is not None
            or any(g.get('owner_reassessment_incident') == OWNER_REASSESSMENT_INCIDENT
                   for g in review.get('preparation_recovery_history', []))
            or not body.evidence_text.strip()
            or review.get('current_request_id') != binding['request_id']
            or latest.get('package', {}).get('request_id') != binding['request_id']
            or latest.get('request_hash') != binding['request_hash']
            or digest(receipt) != binding['receipt_hash']
            or (receipt.get('verdict') or {}).get('decision') != binding['decision']
            or str(body.expected_review_request_id) != binding['request_id']
            or body.expected_review_request_hash != binding['request_hash']):
        raise WorkConflict('not eligible for fixed owner reassessment')
    return True


def proven_pre_send_abort(work, state, body):
    """Only the server's pre-POST abort transition; never an uncertain send."""
    if body.reason != 'aborted_before_send':
        return None
    intent = state.get('intent') or {}
    if (work.kind != 'followup_due'
            or work.error != 'followup_handoff:execution_error:ExecuteRequestError'
            or intent.get('state') != 'aborted_before_send'
            or intent.get('reason_code') != 'fresh_revalidation_failed'
            or intent.get('sent_at') is not None or intent.get('provider_message_id') is not None
            or not intent.get('reserved_at') or state.get('decision') is not None
            or state.get('pre_send_abort_history') is not None):
        return None
    try:
        UUID(intent['intent_id'])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    matches = [r for r in (state.get('review') or {}).get('requests', [])
               if r.get('status') == 'approved'
               and (r.get('package') or {}).get('payload_sha256') == intent.get('payload_sha256')
               and (r.get('receipt') or {}).get('runner_outcome') == 'verdict'
               and ((r.get('receipt') or {}).get('verdict') or {}).get('decision') == 'APPROVE']
    if len(matches) != 1 or not intent.get('payload_sha256'):
        return None
    return {'intent': deepcopy(intent), 'review_request_id': matches[0]['package']['request_id']}


def owner_reassessment_generation(work, state, body):
    """Authenticated operator resolution permits one fresh preparation generation.

    This never supplies a verdict or send permission. The exact former grant and
    review stay immutable; a repeated resolution of the same basis is rejected.
    """
    if body.reason != 'owner_reassessment':
        return None
    previous = state.get('preparation_recovery')
    review = state.get('review') or {}
    requests = review.get('requests') or []
    latest = requests[-1] if requests else {}
    if (work.kind != 'followup_due' or state.get('intent') is not None
            or work.error not in ERRORS | {'followup_handoff:review_rejected', 'followup_handoff:draft_deferred'}
            or (previous is not None and
                (previous.get('policy') != POLICY or previous.get('status') != 'consumed'))
            or body.expected_preparation_recovery_hash != digest(previous)
            or not body.evidence_ref.strip() or not body.evidence_text.strip()):
        raise WorkConflict('owner reassessment requires resolved unchanged preparation')
    request_id = latest.get('package', {}).get('request_id')
    request_hash = latest.get('request_hash')
    receipt_hash = digest(latest.get('receipt')) if latest else None
    initial_pre_review = (work.error == 'followup_handoff:draft_deferred'
                          and previous is None and not requests
                          and review.get('current_request_id') is None
                          and state.get('decision') is None
                          and not review.get('preparation_recovery_history')
                          and not review.get('owner_reassessment_history'))
    if latest and latest.get('status') not in {'rejected', 'invalidated', 'expired'}:
        # Pending review or a live approval remains owned by its current
        # review/execution recovery path, never superseded by owner refill.
        raise WorkConflict('owner reassessment requires a terminal review')
    if ((str(body.expected_review_request_id) if body.expected_review_request_id is not None else None) != request_id
            or body.expected_review_request_hash != request_hash
            or body.expected_review_receipt_hash != receipt_hash
            or (not latest and previous is None and not initial_pre_review)):
        raise WorkConflict('owner reassessment review changed')
    pre_review_resumption = None
    if work.error == 'followup_handoff:draft_deferred' and not initial_pre_review:
        # A drafter may defer BEFORE opening a new review. An authenticated
        # owner can resolve that finding once, rather than reusing an old
        # verdict or endlessly refilling the same consumed generation.
        former_owner = (previous or {}).get('owner_reassessment') or {}
        prior_ids = (previous or {}).get('prior_request_ids') or []
        resolution_hash = digest({'evidence_ref': body.evidence_ref,
                                  'evidence_text': body.evidence_text})
        if (not previous or former_owner.get('schema') != 'owner-reassessment/v1'
                or not previous.get('audit_id') or not former_owner.get('basis_hash')
                or review.get('current_request_id') is not None
                or state.get('decision') is not None
                or former_owner.get('initial_pre_review') is not None
                or former_owner.get('pre_review_resumption') is not None
                or resolution_hash == former_owner.get('operator_resolution_sha256')
                or len(prior_ids) != len(requests)
                or set(prior_ids) != {r.get('package', {}).get('request_id') for r in requests}
                or any((h.get('pre_review_resumption') or {}).get('source_owner_basis_hash')
                       == former_owner['basis_hash']
                       or (h.get('pre_review_resumption') or {}).get('source_grant_audit_id')
                       == previous['audit_id']
                       for h in review.get('owner_reassessment_history', []))):
            raise WorkConflict('owner pre-review resumption requires a new resolved unchanged basis')
        pre_review_resumption = {
            'schema': 'owner-pre-review-resumption/v1',
            'source_grant_audit_id': previous['audit_id'],
            'source_grant_sha256': digest(previous),
            'source_owner_basis_hash': former_owner['basis_hash'],
        }
    if (previous and previous.get('owner_reassessment') and not pre_review_resumption and
            (request_id is None or request_id in previous.get('prior_request_ids', []))):
        # A second allowance is not a remedy for the same failed generation.
        # There must be a new review outcome to resolve before another rework.
        raise WorkConflict('owner reassessment requires a new review generation')
    basis_hash = digest({'review_request_id': request_id, 'request_hash': request_hash,
                         'receipt_hash': receipt_hash,
                         'technical_grant_audit_id': previous.get('audit_id') if previous and not latest else None,
                         **({'pre_review_resumption': pre_review_resumption} if pre_review_resumption else {})})
    if any(h.get('basis_hash') == basis_hash for h in review.get('owner_reassessment_history', [])):
        raise WorkConflict('owner reassessment basis already consumed')
    return {'schema': 'owner-reassessment/v1', 'basis_hash': basis_hash,
            **({'initial_pre_review': {'schema': 'owner-pre-review-initial/v1'}} if initial_pre_review else {}),
            **({'pre_review_resumption': pre_review_resumption} if pre_review_resumption else {}),
            'prior_grant_sha256': digest(previous),
            'prior_review_request_id': request_id, 'prior_review_request_hash': request_hash,
            'prior_review_receipt_sha256': receipt_hash,
            'operator_resolution_sha256': digest({'evidence_ref': body.evidence_ref,
                                                 'evidence_text': body.evidence_text})}


def grant(session, principal, work_id, body, provider, *, now=None):
    from src.crm.services import followup_service as service
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    now = now or datetime.now(UTC)
    work = locked_work(session, principal.workspace_id, work_id)
    name, state, mailbox = state_and_mailbox(work)
    service.mailbox_scope(principal, mailbox)
    request_body = body.model_dump(mode='json')
    # Preserve existing command replay digests when new optional CAS fields are absent.
    for key in ('expected_review_request_id', 'expected_review_request_hash', 'expected_review_receipt_hash', 'expected_preparation_recovery_hash'):
        if request_body.get(key) is None:
            request_body.pop(key, None)
    request_digest = digest(request_body)
    prior = session.scalar(select(AuditEvent).where(
        AuditEvent.workspace_id == principal.workspace_id, AuditEvent.command_id == body.command_id))
    if prior:
        if (prior.action != ACTION or prior.entity_id != work.id or prior.actor_id != principal.actor_id
                or prior.details.get('request_digest') != request_digest):
            raise WorkConflict('preparation recovery command conflict')
        return response(work, prior.details['receipt'], True)
    repairing_runtime = technical_repair(work, state, body)
    reassessing_owner = owner_reassessment(work, state, body)
    from src.crm.services.followup_technical_review import validate as validate_technical
    technical_successor = validate_technical(session, work, state, body)
    pre_send_abort = proven_pre_send_abort(work, state, body)
    if body.reason == 'aborted_before_send' and not pre_send_abort:
        raise WorkConflict('not a verified pre-send abort')
    owner_generation = owner_reassessment_generation(work, state, body)
    if owner_generation:
        owner_generation['actor_id'] = str(principal.actor_id)
    previous_allowance = deepcopy(state.get('preparation_recovery'))
    if (work.status != 'failed' or (work.error not in ERRORS and not technical_successor and not pre_send_abort and not owner_generation) or not 1 <= work.attempts <= 3
            or work.lease_token is not None or work.lease_until is not None
            or work.result is not None or work.result_hash is not None
            or (state.get('intent') is not None and not pre_send_abort)
            or (state.get('preparation_recovery') is not None and not (repairing_runtime or reassessing_owner or owner_generation))
            or body.source_key != work.source_key or body.expected_work_digest != work_digest(work)):
        raise WorkConflict('not eligible for preparation recovery')
    _locked_company_lead(session, work.workspace_id, work.lead_id)
    lead = session.get(Lead, work.lead_id)
    if not lead or lead.version != body.expected_lead_version:
        raise WorkConflict('preparation recovery lead changed')
    if name == 'followup_v1':
        obligation = state.get('obligation') or {}
        if (work.kind != 'followup_due' or obligation.get('key') != work.source_key
                or obligation.get('lead_id') != str(work.lead_id)
                or service.obligation_key(work.workspace_id, mailbox, work.lead_id,
                    obligation.get('thread_id'), obligation.get('fu_stage'),
                    obligation.get('anchor_message_id')) != work.source_key):
            raise WorkConflict('preparation recovery binding changed')
        _locked_rows(session, select(Task).where(Task.workspace_id == work.workspace_id, Task.id == work.task_id))
        task = session.get(Task, work.task_id)
        if (not task or work.task_id != service.task_id(work.workspace_id, work.source_key)
                or task.lead_id != work.lead_id or task.status != 'open' or task.task_type != 'email'
                or task.source_rule != service.POLICY_ID or task.version != body.expected_task_version
                or task.version != state.get('task_version') or task.version != work.payload.get('task_version')
                or task.due_at > now):
            raise WorkConflict('preparation recovery task changed or deferred')
        thread_id, anchor_id, own_task = obligation['thread_id'], obligation['anchor_message_id'], str(task.id)
    else:
        if (work.kind != 'sent_review' or work.task_id is not None or body.expected_task_version is not None
                or state.get('lead_id') != str(work.lead_id) or state.get('classification') is not None
                or state.get('decision') is not None or work.payload.get('provider') != 'gmail'
                or work.payload.get('source_scope') != 'mailbox:' + mailbox
                or work.payload.get('message_id') != state.get('provider_message_id')
                or work.payload.get('thread_id') != state.get('thread_id')):
            raise WorkConflict('preparation recovery anchor binding changed')
        thread_id, anchor_id, own_task = state['thread_id'], state['provider_message_id'], None
    snapshot = service.current_context(session, principal, work.lead_id, mailbox, provider, thread_ids=[thread_id])
    if snapshot['context_hash'] != body.context_hash:
        raise FollowupError('preparation_context_changed', 409)
    anchor = service.verified_anchor(snapshot, anchor_id)
    if anchor['thread_id'] != thread_id:
        raise WorkConflict('preparation recovery thread changed')
    assessment = service.assessment(snapshot, anchor, own_task_id=own_task)
    # Classifying existing SENT may record a human hold; it grants no new email.
    anchor_human_hold = (name == 'followup_anchor_v1' and work.kind == 'sent_review'
                         and assessment['disposition'] == 'deferred'
                         and assessment.get('reason_code') == 'human_or_other_obligation')
    if assessment['disposition'] != 'eligible' and not anchor_human_hold:
        raise FollowupError(PREPARATION_SOURCE_CONFLICT_CODES.get(assessment.get('reason_code'), 'preparation_source_reconciliation_required'), 409)
    from src.crm.services.followup_technical_review import same_source
    if technical_successor and not same_source(work, snapshot):
        raise WorkConflict('technical review source requires reconciliation')
    if (technical_successor or pre_send_abort or owner_generation) and (mailbox_readiness(session, work, now)['status'] != 'ready'
            or (assessment.get('not_before') and service.timestamp(assessment['not_before']) > now)):
        raise WorkConflict('technical review mailbox not ready')
    audit_id = uuid4()
    receipt = identity(work) | {'policy': POLICY, 'audit_id': str(audit_id),
        'command_id': str(body.command_id), 'fresh_review_required': True,
        'prior_work_digest': body.expected_work_digest, 'granted_at': now.isoformat()}
    allowance = {'policy': POLICY, 'status': 'available', 'command_id': str(body.command_id),
        'audit_id': str(audit_id), 'evidence_ref': body.evidence_ref, 'evidence_text': body.evidence_text,
        'receipt': receipt, 'crm_manifest_sha256': snapshot['source_versions']['crm_manifest_sha256'],
        'context_hash': body.context_hash, 'from_attempt': work.attempts,
        'prior_request_ids': [r['package']['request_id'] for r in (state.get('review') or {}).get('requests', [])]}
    if owner_generation:
        allowance['owner_reassessment'] = owner_generation
        receipt['owner_reassessment'] = owner_generation
    if technical_successor:
        allowance['technical_review_successor'] = technical_successor
        allowance['actor_id'] = str(principal.actor_id)
        receipt['technical_review_successor'] = technical_successor
    if repairing_runtime:
        allowance['technical_repair_incident'] = RUNTIME_REPAIR_INCIDENT
        allowance['previous_grant_audit_id'] = previous_allowance['audit_id']
        receipt['technical_repair_incident'] = RUNTIME_REPAIR_INCIDENT
    if reassessing_owner:
        allowance['owner_reassessment_incident'] = OWNER_REASSESSMENT_INCIDENT
        allowance['previous_grant_audit_id'] = previous_allowance['audit_id']
        allowance['reassessed_review_request_id'] = str(body.expected_review_request_id)
        receipt['owner_reassessment_incident'] = OWNER_REASSESSMENT_INCIDENT
    state = deepcopy(state)
    if pre_send_abort:
        # The old intent remains immutable evidence. This one-shot preparation
        # allowance creates no send or review authority for the next generation.
        state['pre_send_abort_history'] = [{'schema': 'followup-pre-send-abort/v1',
            **pre_send_abort, 'grant_command_id': str(body.command_id),
            'grant_audit_id': str(audit_id)}]
        state['intent'] = None
        allowance['pre_send_abort_sha256'] = digest(pre_send_abort)
    if previous_allowance is not None and (repairing_runtime or reassessing_owner or owner_generation):
        # Keep the complete former grant in the private review ledger. The
        # public projection redacts this ledger; historical evidence is retained
        # without publishing old operator text or changing the former audit.
        state.setdefault('review', {'requests': []}).setdefault(
            'preparation_recovery_history', []).append(previous_allowance)
    if owner_generation:
        state.setdefault('review', {'requests': []}).setdefault('owner_reassessment_history', []).append(
            owner_generation | {'grant_command_id': str(body.command_id), 'grant_audit_id': str(audit_id),
                                'previous_decision': deepcopy(state.get('decision'))})
    previous_lead_version = state.get('expected_lead_version')
    previous_decision = state.get('decision')
    # Keep former decisions and all review bytes as historical data. New review
    # cannot reuse them and is mandatory before any execute receipt is accepted.
    state['preparation_recovery'] = allowance
    state['expected_lead_version'] = body.expected_lead_version
    state['decision'] = None
    state.setdefault('review', {'requests': []})['current_request_id'] = None
    save_ledger(work, state)
    allowance['claim_digest'] = stable_work(work)
    state['preparation_recovery'] = allowance
    save_ledger(work, state)
    session.add(AuditEvent(id=audit_id, workspace_id=work.workspace_id, command_id=body.command_id,
        actor_id=principal.actor_id, action=ACTION, entity_type='agent_work', entity_id=work.id,
        details={'request_digest': request_digest, 'receipt': receipt,
                 'allowance_sha256': digest(allowance),
                 'previous_error': work.error, 'previous_expected_lead_version': previous_lead_version,
                 'previous_decision': previous_decision,
                 'pre_send_abort': pre_send_abort,
                 'owner_reassessment': owner_generation,
                 'technical_repair_incident': RUNTIME_REPAIR_INCIDENT if repairing_runtime else None,
                 'previous_allowance_sha256': digest(previous_allowance) if (repairing_runtime or reassessing_owner) else None,
                 'owner_reassessment_incident': OWNER_REASSESSMENT_INCIDENT if reassessing_owner else None,
                 'reassessed_review_request_id': str(body.expected_review_request_id) if reassessing_owner else None}))
    work.status, work.available_at, work.updated_at = 'queued', now, now
    session.flush()
    return response(work, receipt, False)


def allowance(work):
    for name in ('followup_v1', 'followup_anchor_v1'):
        value = (work.payload.get(name) or {}).get('preparation_recovery')
        if value is not None:
            return value
    return None


def claim_filter(*, explicit):
    values = [AgentWork.payload[n]['preparation_recovery'] for n in ('followup_v1', 'followup_anchor_v1')]
    present = or_(*(v.is_not(None) & (v.astext != 'null') for v in values))
    available = or_(*(and_(v['policy'].astext == POLICY, v['status'].astext == 'available') for v in values))
    return present, available if explicit else False


def consume_for_claim(session, work, now, *, principal=None, provider_factory=None):
    from src.crm.services.note_plan_service import _locked_company_lead
    value = allowance(work)
    if (not value or value.get('policy') != POLICY or value.get('status') != 'available'
            or work.status != 'queued' or work.lease_token is not None or work.lease_until is not None
            or value.get('from_attempt') != work.attempts or value.get('claim_digest') != stable_work(work)):
        return False
    try:
        audit_id, command_id = UUID(value['audit_id']), UUID(value['command_id'])
    except (ValueError, KeyError, TypeError):
        return False
    event = session.scalar(select(AuditEvent).where(AuditEvent.workspace_id == work.workspace_id, AuditEvent.id == audit_id))
    if (not event or event.action != ACTION or event.entity_id != work.id or event.command_id != command_id
            or event.details.get('allowance_sha256') != digest(value)):
        return False
    # Known no-send mailbox holds must not burn the one-shot allowance. This
    # check is inside the exact claim transaction, under the existing lock.
    if mailbox_readiness(session, work, now)['status'] == 'deferred':
        return False
    _locked_company_lead(session, work.workspace_id, work.lead_id)
    crm, _ = crm_snapshot(session, work.workspace_id, work.lead_id)
    if digest(crm) != value['crm_manifest_sha256']:
        return False
    from src.crm.services.followup_technical_review import fresh_claim
    if not fresh_claim(session, work, value, now, principal, provider_factory):
        return False
    state = deepcopy(work.payload[ledger_name(work)])
    state['preparation_recovery'] = value | {'status': 'consumed', 'consumed_at': now.isoformat()}
    save_ledger(work, state)
    session.add(AuditEvent(workspace_id=work.workspace_id, command_id=uuid5(command_id, 'claim'),
        actor_id=event.actor_id, action='followup.preparation_recovery.consumed', entity_type='agent_work',
        entity_id=work.id, details={'grant_audit_id': str(audit_id), 'policy': POLICY, 'attempt': work.attempts}))
    return True


def review_evidence(state):
    value = state.get('preparation_recovery')
    if not value:
        return None
    if value.get('status') != 'consumed':
        raise WorkConflict('preparation recovery must be explicitly claimed')
    ids = set(value['prior_request_ids'])
    from src.crm.services.followup_expired_handoff import review_evidence as expired_evidence
    continuation = expired_evidence(state)
    ids.update((state.get('expired_handoff') or {}).get('protected_request_ids', []))
    return {'policy': POLICY, 'command_id': value['command_id'], 'audit_id': value['audit_id'],
            **({'expired_handoff': continuation} if continuation else {}),
            'evidence_ref': value['evidence_ref'], 'operator_evidence': value.get('evidence_text', ''),
            'operator_evidence_is_authority': False, 'fresh_review_required': True,
            'prior_findings': [{'request_id': r['package']['request_id'], 'request_hash': r['request_hash'],
                                'status': r['status'], 'receipt': r.get('receipt')}
                               for r in state['review']['requests'] if r['package']['request_id'] in ids]}


def expired_classification_handoff(work, state, grant, now, expected_request_id):
    """Read-only evidence for one unaccepted, expired classification continuation.

    The caller retains the original grant's single recovery identity, counters,
    history and CAS. This never accepts an old verdict or handles email sending.
    """
    if (expected_request_id is None or work.kind != 'sent_review'
            or grant.get('policy') != POLICY or grant.get('status') != 'consumed'
            or work.error not in ('Worker lease expired', 'followup_handoff:review_or_execution_not_finalized')
            or state.get('classification') is not None):
        return None
    review=state.get('review') or {};requests=review.get('requests') or []
    if review.get('current_request_id') != str(expected_request_id):
        return None
    prior=set(grant.get('prior_request_ids') or [])
    post=[r for r in requests if r.get('package',{}).get('request_id') not in prior]
    if not post or not any(r.get('package',{}).get('request_id')==str(expected_request_id) for r in post):
        return None
    # Any recorded verdict/receipt remains authoritative, including prior grants.
    if any(r.get('receipt') is not None or r.get('consumed_at') is not None
            or r.get('status') in ('rejected','approved') for r in requests):
        return None
    evidence=[]
    try:
        consumed=datetime.fromisoformat(grant['consumed_at'].replace('Z','+00:00'))
        for r in post:
            p=r['package'];created=datetime.fromisoformat(p['created_at'].replace('Z','+00:00'))
            expires=datetime.fromisoformat(p['expires_at'].replace('Z','+00:00'))
            if (r.get('status') != 'pending_review' or r.get('verdict') is not None
                    or p.get('purpose') != 'anchor_classification'
                    or p.get('work_id') != str(work.id) or p.get('workspace_id') != str(work.workspace_id)
                    or not all(t.tzinfo is not None for t in (consumed,created,expires))
                    or created < consumed or expires > now or expires <= created
                    or r.get('request_hash') != digest(p)):
                return None
            evidence.append({'request_id':p['request_id'],'request_hash':r['request_hash'],'expires_at':p['expires_at']})
    except (KeyError,TypeError,ValueError):
        return None
    return evidence


# One immutable pre-review GET failure; not a new grant/incident/send policy.
PALOPINA_PREPARE_WORK = '50cfb435-4bbf-538c-bf50-bddf6b2e52c4'
PALOPINA_CONSUMED_AUDIT = '25fc48b6-8b89-49d0-ac31-da13ac93f3d5'
PALOPINA_PREPARE_EVIDENCE = 'e18de13123d5b4cfabeef0408c5ed6b8026ab2a0956e2bc96dbdfbfe38a270a9'


def prepare_get_failure_handoff(work, state, grant, body, principal, original, consumed):
    """Exact original consumed audit + immutable GETprepare witness, no writes."""
    try:
        observed = body.prepare_failure_observed_at
        when = datetime.fromisoformat(grant['consumed_at'].replace('Z', '+00:00'))
        review = state.get('review') or {}
        obligation = state.get('obligation') or {}
        receipt = original.details.get('receipt') or {}
        return bool(
            str(work.id) == PALOPINA_PREPARE_WORK
            and str(work.task_id) == '79f43313-b72b-5a49-94d4-4c509f999d52'
            and str(work.lead_id) == '4c042bb7-e456-5a23-89a9-6413e64aebcd'
            and work.kind == 'followup_due' and work.attempts == 1
            and work.error == 'followup_handoff:execution_error:AdapterError'
            and grant.get('policy') == POLICY and grant.get('status') == 'consumed'
            and grant.get('command_id') == '3a3605b9-b94c-4772-82ce-5685b29e9474'
            and grant.get('audit_id') == 'e5858de7-571a-4f69-9836-ac6d8a884bae'
            and grant.get('from_attempt') == 1 and grant.get('prior_request_ids') == []
            and obligation.get('key') == work.source_key == 'fu:v1:89f00de072cce880e0ba1fbf4cdedcdb6d05564f2ad80b5c60d9fdfeba25857e'
            and obligation.get('thread_id') == '1a0e84ff6644d35b'
            and obligation.get('anchor_message_id') == '1a11d29219318215'
            and body.expected_prepare_read_failure == 'provider_read_failed'
            and body.prepare_failure_evidence_sha256 == PALOPINA_PREPARE_EVIDENCE
            and observed.tzinfo is not None and when.tzinfo is not None
            and when <= consumed.created_at <= observed <= work.updated_at
            and 0 <= (consumed.created_at-when).total_seconds() <= 1
            and 0 <= (work.updated_at-observed).total_seconds() <= 60
            and work.result_hash is None and work.result is None
            and state.get('intent') is None and state.get('decision') is None
            and state.get('classification') is None and state.get('task_version') == 1
            and state.get('expected_lead_version') == 6
            and review.get('status') is None and not review.get('requests')
            and not any(review.get(k) is not None for k in ('current_request_id','request_id','reviewer_receipt_id','receipt'))
            and original.id == UUID(grant['audit_id'])
            and original.command_id == UUID(grant['command_id'])
            and original.action == ACTION and original.workspace_id == principal.workspace_id
            and original.entity_id == work.id and original.actor_id == principal.actor_id
            and receipt.get('work_id') == str(work.id) and receipt.get('source_key') == work.source_key
            and str(consumed.id) == PALOPINA_CONSUMED_AUDIT
            and consumed.command_id == uuid5(UUID(grant['command_id']), 'claim')
            and consumed.action == 'followup.preparation_recovery.consumed'
            and consumed.workspace_id == principal.workspace_id and consumed.entity_id == work.id
            and consumed.actor_id == principal.actor_id
            and consumed.details == {'grant_audit_id':grant['audit_id'],'policy':POLICY,'attempt':1})
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


# Individually proven original false rejection. No population-based admission.
AMERICOLD_FALSE_REJECT_CERTIFICATE = {'work_id': 'a53c66b0-938f-55aa-9866-4928c7a98b14', 'task_id': '5023d55c-d370-5f94-8a28-3b4687ba2157', 'lead_id': '4b715ac1-fbeb-5927-b8a9-6c1eef7166e5', 'source_key': 'fu:v1:6818a5a161fcf77fdebc99271988f4e45ddd2cccc6f06ace55ae9c3398368cc7', 'workspace_id': '339f8411-5023-4b6c-ae0e-202609050001', 'actor_id': '8eb857f6-29b1-5f53-961d-e037d6f2d270', 'grant_command_id': '86c6e920-3d71-4264-96fa-db1f36270710', 'grant_audit_id': '5f8ef070-f54d-429b-96a3-acf4a4159d4f', 'consumed_audit_id': 'ba14d589-4767-49cf-8d12-ebfc495a0d1e', 'consumed_at': '2026-10-11T22:20:34.002545+00:00', 'updated_at': '2026-10-11 22:20:45.921386+00:00', 'payload_hash': '1f911f62d2443cf33c0e678be9f5e6346ba45d9781711ca98db2261ee8a66705', 'journal_sha256': '91f62cf94652a9a80aab11392212d1fb6f70f74d1809a76e31fc711844b6907f', 'runtime_input_sha256': '888effdea798bbeef6973fccbab89eed4e39cb11a3f11136d5c243be1e539993', 'draft_fingerprint': '282dc952558474ff549949203c902489256cc81c80fa57f6345ecf5983fba9b9', 'raw_sha256': '55f6db9773f7d43ca83a069026b148e86cc875104aee722eba77b2d44237390f', 'original_context_hash': '07fe99d716791920da324f0040af31ccfa5f96d0a3338cc2b1cc3b76ece0831a', 'original_validator_sha256': '1b3ad72e069794bd7ec57ef5b9429643dc13cd72ea796ae0e2361d1a08065a08', 'original_http_log_sha256': '67699ee45f6a78db5efd0551cc9c27547838f7d48275ef6210ce3bc5f636599e', 'actual_method': 'POST', 'phase': 'prepare', 'http_status': 422, 'reason': 'signature_or_font_changed', 'correction': 'exact_gmail_signature_envelope', 'fresh_review_required': True, 'new_grant': False}
AMERICOLD_FALSE_REJECT_CERTIFICATE_SHA256 = 'e3baf2e49b62efc35c8e0e029bcc21b8b6c8765ec68c098d415251df7da5c0b3'

AMERICOLD_PEER_FALSE_REJECT_CERTIFICATES = {'aa160eaf-556a-5948-a07c-699fd607fc8a': ({'work_id': 'aa160eaf-556a-5948-a07c-699fd607fc8a', 'task_id': '2989c212-0cd7-589c-b01f-fe1e19845108', 'lead_id': 'e7afdf8f-506e-5c9d-b43e-3d2332a07078', 'source_key': 'fu:v1:ebc55425bcbb68f67f07b963513a245f3ec30250fc10479a2fe44d57c8c6668e', 'workspace_id': '339f8411-5023-4b6c-ae0e-202609050001', 'actor_id': '8eb857f6-29b1-5f53-961d-e037d6f2d270', 'grant_command_id': '814384d4-21a3-4335-8ff5-7ddadca05741', 'grant_audit_id': 'e1610e47-43fc-468b-9e90-2506b3cae67c', 'consumed_audit_id': 'd444fb04-2c79-4b31-9ff4-7c93665e8e2c', 'consumed_at': '2026-10-12T09:35:36.541749+00:00', 'updated_at': '2026-10-12 09:35:50.413230+00:00', 'payload_hash': '76bb0a7b19bf9add54fab2acb633596145e8f7c293b388e85769614e081edbfc', 'grant_sha256': '1fb0fdb2c79704d4ef877d60b461c70ad3ebb08084858ed228fc63bc30fe69ae', 'original_audit_details_sha256': 'd06f99e4173775b29d426130ccecb219afa34d30200c5f01c32a3092a1ddf9f1', 'original_audit_created_at': '2026-10-12 09:35:32.144849+00:00', 'consumed_audit_created_at': '2026-10-12 09:35:36.582030+00:00', 'worker_id': 'hermes-head-of-sales-mac-mini', 'expected_lead_version': 4, 'task_version': 1, 'task_due_at': '2026-10-12 09:00:00+00:00', 'journal_sha256': '7cf9bd37ff4a5278d5e161e5919965f73fabad6a980e3e3ab3f9bd35b7a61e2d', 'journal_filename': 'aa160eaf-556a-5948-a07c-699fd607fc8a-c1a51e429fca4fc5.json', 'journal_at': '2026-10-12T09:35:49.757204+00:00', 'journal_immutable_method': 'GET', 'runtime_input_sha256': 'c75401a0d676cbe819e577a281b1c46b12b3f595055ac15f90de9d198f1c3c24', 'runtime_timestamp': 1791796531.257258, 'draft_fingerprint': 'a1c742659f950f446ee1bd89b08c1448c8a9cfb9b958316f35c12a49f25e6276', 'raw_sha256': '51b7a41085233c124fab4e8735de8851db3b553105ff0053ec402fbf82260f0a', 'original_context_hash': 'b79e40cfbcbff606c7734c4db80e4c4d87da61b060ccceb1b522e2e3e41fb189', 'source_versions': {'crm_manifest_sha256': 'f7f82c68f661fe43b907d562cba5fb479fca92c2bd045e2779dffb5db2c6f002', 'lead_version': 4, 'provider_manifest_sha256': 'fab8e66b07df456adcba86aea53f8fbd8dc9a841d89f909fb6b2ca04bab29c0b'}, 'original_validator_sha256': '1b3ad72e069794bd7ec57ef5b9429643dc13cd72ea796ae0e2361d1a08065a08', 'original_container': 'e6961af85797', 'original_source_manifest_sha256': 'bdab7285eb3d377e06bcb7b01ed68d87759a270bb620971c5a195f383b4a7bf0', 'original_http_log_sha256': 'b53b1f88fb6c318dede83f2082c0e38efa0418072bb6125294fbb4c3a3f2aa52', 'actual_method': 'POST', 'phase': 'prepare', 'http_status': 422, 'reason': 'signature_or_font_changed', 'correction': 'exact_gmail_signature_envelope', 'fresh_review_required': True, 'new_grant': False, 'evidence_sha256': {'canonical-private.json': '84dee46f4f43b9634344989c7ec982ad5155dfc8b9120c88664558926d7b8628', 'a1c742659f950f446ee1bd89b08c1448c8a9cfb9b958316f35c12a49f25e6276-input-private.json': '597eadb57ca8df68723fcf43387a73dff80e5bd2d012c6c1ca4bd83734cf7e24', 'a1c742659f950f446ee1bd89b08c1448c8a9cfb9b958316f35c12a49f25e6276-draft-private.json': '19e888ef5b23888846e565b56f5bdab54ba8389cc157f789be5a1fed8567a390', 'followup-handoff-journal--aa160eaf-556a-5948-a07c-699fd607fc8a-c1a51e429fca4fc5.json': '7cf9bd37ff4a5278d5e161e5919965f73fabad6a980e3e3ab3f9bd35b7a61e2d', 'followup-preparation-journal--814384d4-21a3-4335-8ff5-7ddadca05741.json': '6a5f6bd719ee63d6c9774b8dd704a2c4d559585a3b739743490eb4af51437b99', 'retained-index-private.json': '1d34662150d4a998e6fa633d2ac77ab3a31d0b8eaaf36b4a7bcf960ee374a47e', 'http-evidence-private.json': 'afd3348a592c784cf72737faffcefb1e8757f49fcd4ab225c7e9ab7230669581', 'original-http-private.log': 'b53b1f88fb6c318dede83f2082c0e38efa0418072bb6125294fbb4c3a3f2aa52', 'pure-original-validation-private.json': '7f0db110767c9d9a8d8e50796b5fcdc7ad0f7fa3747cfdbcb488c8e577228c3d', 'current-correction-certification.json': '4f804022d18f9b5f81f1c72639b56e11971eeebd61be9d2ad91a98d75b621acd', 'manifest-private.json': 'bdab7285eb3d377e06bcb7b01ed68d87759a270bb620971c5a195f383b4a7bf0'}}, 'f3796489225e497feef639cab122194c4a2bae36bd8e0e40a24e56a3ac4d5c29'), 'c71ff044-dab0-509a-a6ba-c566452e75bd': ({'work_id': 'c71ff044-dab0-509a-a6ba-c566452e75bd', 'task_id': 'ef402282-7d41-5df9-a8ec-375f1ff1fd70', 'lead_id': 'd225ff74-ab78-5e33-8252-6fe9db99b487', 'source_key': 'fu:v1:317babfa575e1308a9e917e37a6e352e72d7c070f41af7c0db8aa4b0d0c328c5', 'workspace_id': '339f8411-5023-4b6c-ae0e-202609050001', 'actor_id': '8eb857f6-29b1-5f53-961d-e037d6f2d270', 'grant_command_id': 'a4a97629-f68f-4702-b163-b695d1143cc7', 'grant_audit_id': '84807691-11b5-4347-8d61-3617aa6e9161', 'consumed_audit_id': '06652831-6eff-44a7-91c2-f8130da5e6f2', 'consumed_at': '2026-10-12T09:40:31.209075+00:00', 'updated_at': '2026-10-12 09:40:43.455176+00:00', 'payload_hash': '15cfefda29d311147b67f7a6bda6d8a64cd6f3bc3c6a774628117cd682789297', 'grant_sha256': 'd728a1c0a9aaac40f02dd48f70d2196267771c16ea49acb1970c647da9917c0f', 'original_audit_details_sha256': '7428d12b444efb2f2c06dd318695fff3662988355ed66a7da1d318d9b43dcd2a', 'original_audit_created_at': '2026-10-12 09:40:26.647000+00:00', 'consumed_audit_created_at': '2026-10-12 09:40:31.235262+00:00', 'worker_id': 'hermes-head-of-sales-mac-mini', 'expected_lead_version': 4, 'task_version': 1, 'task_due_at': '2026-10-12 09:00:00+00:00', 'journal_sha256': 'e42e5dd4d63c1db3b118e818c01719a4868f4ff2cbe02001001c4bbb6bfb5978', 'journal_filename': 'c71ff044-dab0-509a-a6ba-c566452e75bd-e55c7f3c9758544b.json', 'journal_at': '2026-10-12T09:40:42.847519+00:00', 'journal_immutable_method': 'GET', 'runtime_input_sha256': '1b25f91009759baa2bdd483e18007325044a15a4fe1b7dba725e7dd2cf60048d', 'runtime_timestamp': 1791796569.26894, 'draft_fingerprint': '216b20302c3740fad7b9f5676f3e0f1c38d39cd4fecbae4dd6bc7ce6b3e7ceed', 'raw_sha256': 'd19f3d955d2f7a420a602cf387cd4371f77f263b7268103ef8b51ccdc1849555', 'original_context_hash': '0daff35370cf21a2925a97be6420cc87692e2e48e461703e1f28c7b1c781babe', 'source_versions': {'crm_manifest_sha256': 'f02121338048331ee1d75ad616e803374e9b559e21e11c73110e0e90b9d97f58', 'lead_version': 4, 'provider_manifest_sha256': '63604512db485da2579af1e3bcdc42ccfeee63ac6965b0e10e42319883eb6299'}, 'original_validator_sha256': '1b3ad72e069794bd7ec57ef5b9429643dc13cd72ea796ae0e2361d1a08065a08', 'original_container': 'e6961af85797', 'original_source_manifest_sha256': 'bdab7285eb3d377e06bcb7b01ed68d87759a270bb620971c5a195f383b4a7bf0', 'original_http_log_sha256': 'b43dff4594d50e1a23261a594870b3cbb6d2d307ad850016b253b1d9206eb520', 'actual_method': 'POST', 'phase': 'prepare', 'http_status': 422, 'reason': 'signature_or_font_changed', 'correction': 'exact_gmail_signature_envelope', 'fresh_review_required': True, 'new_grant': False, 'evidence_sha256': {'canonical-private.json': 'cb16af335471dd8cbd682d968c3e9f3d9abee29c4621cfe4c68d00a71c2e6026', '216b20302c3740fad7b9f5676f3e0f1c38d39cd4fecbae4dd6bc7ce6b3e7ceed-input-private.json': 'bf77d30616b4217a7356e7ae2cd994f194c45f01b1473caccadd19658cc41810', '216b20302c3740fad7b9f5676f3e0f1c38d39cd4fecbae4dd6bc7ce6b3e7ceed-draft-private.json': 'f3c264b221e172c438e8cf5f8c5b8c88afe7738854f4e78fe824fc3dec58e885', 'followup-handoff-journal--c71ff044-dab0-509a-a6ba-c566452e75bd-e55c7f3c9758544b.json': 'e42e5dd4d63c1db3b118e818c01719a4868f4ff2cbe02001001c4bbb6bfb5978', 'followup-preparation-journal--a4a97629-f68f-4702-b163-b695d1143cc7.json': '37c98741a04c7edc91049a691cf31ab15c4e7d2931e8561bb323d645a535a823', 'retained-index-private.json': '66325180db8ee81ad515eb025402a78543726b4ea233209977610e87cd2e48a4', 'http-evidence-private.json': '8489d689b90bee05252d4bd4370f7d2c941f0ff40a130374b933524d9ae26d29', 'original-http-private.log': 'b43dff4594d50e1a23261a594870b3cbb6d2d307ad850016b253b1d9206eb520', 'pure-original-validation-private.json': '438ea0d4336e10db9d8590faa2e6693d17ba4aebda08b6ec342a2c273df5d7d1', 'current-correction-certification.json': '7368db5373a28a08267971559876b36f72030d29d81d8b82facb1441bfa3700b', 'manifest-private.json': 'bdab7285eb3d377e06bcb7b01ed68d87759a270bb620971c5a195f383b4a7bf0'}}, '15a3a7fd4bf00353d001d2cfda7cfa259c6008619bdf6317be3670444d67b86d'), 'fe79b151-9889-585c-91d4-e124441ce129': ({'work_id': 'fe79b151-9889-585c-91d4-e124441ce129', 'task_id': 'f143a95d-9724-5b63-8efb-277421d36cf8', 'lead_id': 'fd15e7b4-3208-5b10-b5f3-2910e8c64fa3', 'source_key': 'fu:v1:ef28658a4af53fc3e3e6aebd0f80836a1898eaa009a12c6c40093a91fc7a00ed', 'workspace_id': '339f8411-5023-4b6c-ae0e-202609050001', 'actor_id': '8eb857f6-29b1-5f53-961d-e037d6f2d270', 'grant_command_id': '7c39c67c-2be9-4c8c-96db-8be5231336fc', 'grant_audit_id': '2bd3e7c2-2759-4d1a-a473-f9247848b7c8', 'consumed_audit_id': '58f685f7-60d0-4e79-86e0-bcbc392a63a0', 'consumed_at': '2026-10-13T17:35:33.664170+00:00', 'updated_at': '2026-10-13 17:35:46.195854+00:00', 'payload_hash': '628aae8ec9e8745f4372f807b254362073c5dd5c6b1544df5c0d8a97cdfd1f7b', 'grant_sha256': '48c8c4c4a4d2f6f04ffff57f22e60c91685e41665e65947c79c1fa88ceba5d07', 'original_audit_details_sha256': 'c7a9483c5d579649a963b1928d7eb6b4ce3b3e42de91d72fd6627fcad2ad9b18', 'original_audit_created_at': '2026-10-13 17:35:28.964666+00:00', 'consumed_audit_created_at': '2026-10-13 17:35:33.690794+00:00', 'worker_id': 'hermes-head-of-sales-mac-mini', 'expected_lead_version': 8, 'task_version': 1, 'task_due_at': '2026-10-13 17:11:37+00:00', 'journal_sha256': 'bf396bb78877ca88ad3ef7fc37fb10698f7a03b494979b4304ee918ec9066ca4', 'journal_filename': 'fe79b151-9889-585c-91d4-e124441ce129-dd00e8b7f2b80717.json', 'journal_at': '2026-10-13T17:35:45.647763+00:00', 'journal_immutable_method': 'GET', 'runtime_input_sha256': '6804ae3c5b869c8237121857bdd655eb77b641e6beecf3c10aef0ef083880caa', 'runtime_timestamp': 1791911732.082624, 'draft_fingerprint': '30c1ef1587e9cf531d68e099c0e0b24da2cf8dae021fe842122259776678ccb4', 'raw_sha256': '2c6526eb768a61e81e7f4f80cc4a063e55d4a84b6e789bddf57aef3313002221', 'original_context_hash': '6bfbbf549a252f958eafe6c50d91bfe3c729e65be5e982787bf30ebf88b0e7b6', 'source_versions': {'crm_manifest_sha256': 'f86acdd611fb5aabe5101d7c60157cfc681acb92872b8d2b47964dd4bce580c8', 'lead_version': 8, 'provider_manifest_sha256': 'e5b6b0957e3e6563517a3fd3012c664708d3ee1bac5e092782bceffe550df16c'}, 'original_validator_sha256': '1b3ad72e069794bd7ec57ef5b9429643dc13cd72ea796ae0e2361d1a08065a08', 'original_container': '6f3bc6e26b45', 'original_source_manifest_sha256': 'e2f5d772b7f143e5b383b80d7892db8f485feda532c102a4ba541597dc20a3d1', 'original_http_log_sha256': '17c4454e385e4241c6784663a2ac6e1edb58c9e88d600bdf43c978b88f720ac1', 'actual_method': 'POST', 'phase': 'prepare', 'http_status': 422, 'reason': 'signature_or_font_changed', 'correction': 'exact_gmail_signature_envelope', 'fresh_review_required': True, 'new_grant': False, 'evidence_sha256': {'canonical-private.json': 'd477536f2521e4fad33f327e1510f6edc855bab11aceb390ffd74ac90557d710', '30c1ef1587e9cf531d68e099c0e0b24da2cf8dae021fe842122259776678ccb4-input-private.json': '17c966ba7e6536fd496fd268b6e181d9ac0c0d5cc67139ec42f073ec32da69ae', '30c1ef1587e9cf531d68e099c0e0b24da2cf8dae021fe842122259776678ccb4-draft-private.json': '27b4ef6864ff368f7895dac31183ca44d16d72ed6536f8b3c6d2bc2e2bc03b40', 'followup-handoff-journal--fe79b151-9889-585c-91d4-e124441ce129-dd00e8b7f2b80717.json': 'bf396bb78877ca88ad3ef7fc37fb10698f7a03b494979b4304ee918ec9066ca4', 'followup-preparation-journal--7c39c67c-2be9-4c8c-96db-8be5231336fc.json': '43119273a1dc0b27867c9aa0f62a65e053cf08570f14cb65297d76f3efb11db3', 'retained-index-private.json': '3dace936067e2bb07efbcc5473e589c83892efe37ad3d98bd37a1f2005eed552', 'http-evidence-private.json': '1690159e18c6df129476cf621a49cc0e43190ec0deef897f85996f00425977d4', 'original-http-private.log': '17c4454e385e4241c6784663a2ac6e1edb58c9e88d600bdf43c978b88f720ac1', 'pure-original-validation-private.json': '74b0627e2c3f55e3251b5aa100009e2862dc905ca04d4020774bb62cc2a39049', 'current-correction-certification.json': '42dacb11161663111d1b31b409d9b07084879139f1a4c1987ff20679cfa2eec8', 'manifest-private.json': 'e2f5d772b7f143e5b383b80d7892db8f485feda532c102a4ba541597dc20a3d1'}}, '329c290efb7ea32cfb80128b022d5ba1847800ea6b41c7696e902b5fb91193ff')}
AMERICOLD_FALSE_REJECT_CERTIFICATES = {AMERICOLD_FALSE_REJECT_CERTIFICATE["work_id"]: (AMERICOLD_FALSE_REJECT_CERTIFICATE, AMERICOLD_FALSE_REJECT_CERTIFICATE_SHA256), **AMERICOLD_PEER_FALSE_REJECT_CERTIFICATES}

def certified_prepare_false_reject(work, state, grant, body, principal, original, consumed):
    """Pure fixed original evidence gate; retains the original consumed allowance."""
    c, certificate_sha256 = AMERICOLD_FALSE_REJECT_CERTIFICATES.get(str(work.id), (None, None))
    if c is None:
        return False
    try:
        review = state.get('review') or {}
        return bool(
            str(work.id) == c['work_id'] and str(work.task_id) == c['task_id']
            and str(work.lead_id) == c['lead_id'] and work.source_key == c['source_key']
            and str(work.workspace_id) == str(principal.workspace_id) == c['workspace_id']
            and str(principal.actor_id) == c['actor_id']
            and work.kind == 'followup_due' and work.status == 'failed' and work.attempts == 1
            and work.error == 'followup_handoff:execution_error:AdapterError'
            and work.updated_at == datetime.fromisoformat(c['updated_at'])
            and digest(work.payload) == c['payload_hash']
            and work.lease_token is None and work.lease_until is None
            and work.result is None and work.result_hash is None
            and state.get('intent') is None and state.get('decision') is None
            and not review.get('requests') and not review.get('current_request_id')
            and grant.get('policy') == POLICY and grant.get('status') == 'consumed'
            and grant.get('command_id') == c['grant_command_id'] and grant.get('audit_id') == c['grant_audit_id']
            and grant.get('consumed_at') == c['consumed_at'] and grant.get('prior_request_ids') == []
            and body.false_reject_certificate_sha256 == certificate_sha256
            and (str(work.id) not in AMERICOLD_PEER_FALSE_REJECT_CERTIFICATES or (
                digest(grant) == c['grant_sha256']
                and digest(original.details) == c['original_audit_details_sha256']
                and original.created_at == datetime.fromisoformat(c['original_audit_created_at'])
                and consumed.created_at == datetime.fromisoformat(c['consumed_audit_created_at'])
                and work.worker_id == c['worker_id']
                and state.get('task_version') == c['task_version']
                and state.get('expected_lead_version') == c['expected_lead_version']
                and review.get('status') is None
                and not any(review.get(k) is not None for k in ('request_id','reviewer_receipt_id','receipt'))))
            and original.id == UUID(c['grant_audit_id']) and original.command_id == UUID(c['grant_command_id'])
            and original.action == ACTION and original.workspace_id == principal.workspace_id
            and original.actor_id == principal.actor_id and original.entity_id == work.id
            and (original.details.get('receipt') or {}) == grant['receipt']
            and consumed.id == UUID(c['consumed_audit_id'])
            and consumed.command_id == uuid5(UUID(c['grant_command_id']), 'claim')
            and consumed.workspace_id == principal.workspace_id and consumed.actor_id == principal.actor_id
            and consumed.entity_id == work.id and consumed.action == 'followup.preparation_recovery.consumed'
            and consumed.details == {'grant_audit_id': c['grant_audit_id'], 'policy': POLICY, 'attempt': 1})
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def validate_false_reject_fresh_context(session, principal, work, body):
    """Fresh canonical context/CAS, never original body text as instructions."""
    import os
    from src.crm.connectors.followup_email import GmailProvider
    from src.crm.services import followup_service as service
    obligation = work.payload['followup_v1']['obligation']
    credentials = os.environ.get('GOOGLE_GMAIL_CREDENTIALS_FILE')
    if not credentials:
        raise WorkConflict('False reject context capability unavailable')
    context = service.current_context(session, principal, work.lead_id, obligation['mailbox'],
        GmailProvider(credentials_file=credentials), thread_ids=[obligation['thread_id']])
    anchor = service.verified_anchor(context, obligation['anchor_message_id'])
    if (context['context_hash'] != body.false_reject_context_hash
            or context['source_versions']['lead_version'] != work.payload['followup_v1']['expected_lead_version']
            or anchor['thread_id'] != obligation['thread_id']
            or service.assessment(context, anchor, own_task_id=str(work.task_id))['disposition'] != 'eligible'):
        raise WorkConflict('False reject fresh context changed or held')
    return context['context_hash']


def false_reject_recovery_preflight(session, principal, work):
    """Three fixed peers only: cheap work/audit/task checks before Google."""
    from types import SimpleNamespace
    from src.crm.services.agent_work_service import lead_is_suppressed
    binding = AMERICOLD_PEER_FALSE_REJECT_CERTIFICATES.get(str(work.id))
    if binding is None:
        return False
    c, certificate_sha256 = binding
    state = (work.payload or {}).get('followup_v1') or {}
    grant = state.get('preparation_recovery') or {}
    original = session.get(AuditEvent, UUID(c['grant_audit_id']))
    consumed = session.get(AuditEvent, UUID(c['consumed_audit_id']))
    if not certified_prepare_false_reject(work, state, grant,
            SimpleNamespace(false_reject_certificate_sha256=certificate_sha256), principal, original, consumed):
        return False
    lead = session.scalar(select(Lead).where(Lead.workspace_id == principal.workspace_id, Lead.id == work.lead_id))
    task = session.scalar(select(Task).where(Task.workspace_id == principal.workspace_id, Task.id == work.task_id))
    now = datetime.now(UTC)
    return bool(lead is not None and task is not None
        and lead.version == state['expected_lead_version'] and not lead_is_suppressed(session, lead)
        and task.workspace_id == work.workspace_id and task.lead_id == work.lead_id
        and task.task_type == 'email' and task.source_rule == 'ordinary-followup/v1'
        and task.status == 'open' and task.version == state['task_version']
        and task.due_at == datetime.fromisoformat(c['task_due_at'])
        and task.due_at <= now and work.available_at <= now)
