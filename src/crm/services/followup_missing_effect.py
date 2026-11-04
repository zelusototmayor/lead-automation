"""Bounded Alcont FU4 repair, called only by canonical fresh reconcile.

No provider capability, receipt creation, send, predecessor reopening or review
bypass. Historical authenticated review + exact SENT establishes what happened;
current full context/CAS establishes whether a successor may be materialized.
The caller owns the transaction. Missing private history always fails closed.
"""
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
from uuid import UUID, uuid5

from sqlalchemy import or_, select

from src.crm.connectors.followup_email import decode_payload, receipt_matches
from src.crm.persistence.models import (
    Account, Activity, AgentWork, AuditEvent, Contact, Lead, Meeting, OutboxEvent, Proposal, SourceIdentity, Task,
)
from src.crm.services.followup_context import digest, dto
from src.crm.services.followup_policy import POLICY_ID, obligation_key, task_id
from src.crm.services.followup_review import FollowupError, audit, save_ledger, timestamp, utc
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows

WORKSPACE_ID = UUID('339f8411-5023-4b6c-ae0e-202609050001')
SOURCE_WORK_ID = UUID('b9c25fb8-04d5-5405-b045-76ebf2b040ca')
SOURCE_TASK_ID = UUID('57de0f62-bb40-5991-8b32-b7b55ac626e4')
LEAD_ID = UUID('19d319ff-af07-5db2-9aaf-5c09d2740c1b')
MAILBOX = 'zelu@zelusottomayor.com'
MESSAGE_ID = '1a153eb7387327be'
THREAD_ID = '1a0ab17354519f46'
POLICY = 'alcont-missing-fu4/v1'


def require(condition, code):
    if not condition:
        raise FollowupError(code)


def _lock_snapshot(session, workspace, context):
    """Fence existing human edits as well as FK-backed inserts, then compare."""
    crm = context['crm']
    account = crm['account'] or {}
    groups = (
        (Account, account.get('records', [])),
        (Lead, crm['lead']['related_leads']),
        (Contact, crm['contacts']),
        (Task, crm['tasks_all_statuses']),
        (Activity, crm['activities']),
        (Meeting, account.get('meetings', [])),
        (Proposal, account.get('proposals', [])),
        (SourceIdentity, crm['lead']['sources']),
    )
    for model, expected in groups:
        if not expected:
            continue
        actual = _locked_rows(session, select(model).where(
            model.workspace_id == workspace,
            model.id.in_([UUID(item['id']) for item in expected]),
        ).order_by(model.id))
        require(digest(sorted([dto(row) for row in actual], key=lambda r: r['id']))
                == digest(sorted(expected, key=lambda r: r['id'])), 'context_changed')


def _historical_payload(source, anchor_state, anchor):
    state = source.payload['followup_v1']
    obligation, intent, decision = state['obligation'], state['intent'], state['decision']
    own = anchor_state['decision']
    require(source.kind == 'followup_due' and source.status == 'completed'
            and source.task_id == SOURCE_TASK_ID and source.lead_id == LEAD_ID
            and state['policy_id'] == POLICY_ID and state['task_version'] == 1
            and intent['state'] == 'crm_applied'
            and own['source_work_id'] == str(source.id)
            and own['intent_id'] == intent['intent_id']
            and own['observation_version'] == digest(anchor)
            and intent['provider_message_id'] == anchor['id']
            and intent['sent_at'] == anchor['sent_at']
            and obligation['mailbox'] == MAILBOX and obligation['thread_id'] == THREAD_ID
            and obligation['lead_id'] == str(LEAD_ID) and obligation['fu_stage'] == 3,
            'historical_receipt_binding_mismatch')
    require(source.source_key == obligation['key'] == obligation_key(
        source.workspace_id, MAILBOX, LEAD_ID, THREAD_ID, 3, obligation['anchor_message_id']),
        'historical_obligation_mismatch')
    items = [item for item in state.get('review', {}).get('requests', [])
             if item.get('package', {}).get('request_id') == decision.get('review_request_id')]
    require(len(items) == 1, 'historical_review_unavailable')
    item = items[0]
    package, receipt = item['package'], item.get('receipt') or {}
    verdict = receipt.get('verdict') or {}
    require(item['status'] == 'approved' and bool(item.get('consumed_at'))
            and receipt.get('id') == decision['reviewer_receipt_id']
            and receipt.get('runner_outcome') == 'verdict'
            and verdict.get('decision') == 'APPROVE'
            and verdict.get('conditions') == [] and verdict.get('risk_flags') == []
            and bool(receipt.get('reviewer_session_id'))
            and bool(package.get('reviewer_actor_id'))
            and receipt.get('authenticated_actor_id') == package['reviewer_actor_id']
            and receipt.get('received_at') == item['consumed_at'] == decision['reviewed_at']
            and timestamp(package['created_at']) <= timestamp(item['consumed_at'])
            < timestamp(package['expires_at'])
            and timestamp(item['consumed_at']) <= timestamp(intent['reserved_at'])
            and package['work_id'] == str(source.id)
            and package['workspace_id'] == str(source.workspace_id)
            and package['mailbox'] == MAILBOX and package['obligation_key'] == source.source_key
            and package['expected_task_version'] == state['task_version']
            and package['context']['obligation'] == obligation
            and package['context_hash'] == decision['context_hash'] == digest(package['context'])
            and digest(package) == item['request_hash'] == decision['request_hash'],
            'historical_review_binding_mismatch')
    raw = decode_payload(package['payload_raw_b64'])
    require(hashlib.sha256(raw).hexdigest() == package['payload_sha256']
            == decision['payload_sha256'] == intent['payload_sha256'], 'historical_payload_changed')
    # Expiry/current reviewer configuration is deliberately NOT send authority.
    # This is historical evidence only. A future FU4 still needs a fresh review.
    return raw, obligation, intent, decision


def _verified_manual_completion(session, task, source):
    """Only Main's exact supported complete-task reconciliation, not any edit.

    Pins are canonical DTO hashes from read-only serving history, cross-checked
    against the retained command348391a4 and TaskCommandService.complete. They
    bind the entire task v2, manual activity, actor, command, audit and outbox
    event. Delivery bookkeeping of the outbox may advance independently. This
    does not grant send/review authority or mutate any predecessor/history.
    """
    if (digest(dto(task)) != '918dfc9df7d03c03048449af4080ea82263e8c47c6449e0ef41e732728e8b2de'
            or source.result_hash != '7130b8407c80b176e8dab47a691dfba2d1377fb3be0db2be767f36e5af02f99e'
            or source.payload['followup_v1']['intent'].get('reason_code') != 'receipt_only_human_change'):
        return False
    activities = _locked_rows(session, select(Activity).where(
        Activity.workspace_id == WORKSPACE_ID, Activity.id == task.completion_activity_id))
    audits = _locked_rows(session, select(AuditEvent).where(
        AuditEvent.workspace_id == WORKSPACE_ID, AuditEvent.entity_id == SOURCE_TASK_ID))
    events = _locked_rows(session, select(OutboxEvent).where(
        OutboxEvent.workspace_id == WORKSPACE_ID,
        OutboxEvent.command_id == UUID('348391a4-3535-4490-a418-f1772c245969')))
    event_fields = ('id', 'workspace_id', 'command_id', 'semantic_hash', 'event_type',
                    'aggregate_type', 'aggregate_id', 'payload', 'created_at')
    return (len(activities) == len(audits) == len(events) == 1
            and digest(dto(activities[0])) == 'e33a60a530c52118449f4e1cb8889a0e64c3b3db58430df015f55010cef244fd'
            and digest(dto(audits[0])) == 'a59e2f2625bd82fd895e18420d6f4a90d36a0f51a589bfa94a1d7739a0aa1d85'
            and digest({k: dto(events[0])[k] for k in event_fields})
            == '1d7879f9327bb817fd2228823f09ca3c3b50de07faf62c9318cd1f40ede42d8a')


def reconcile_alcont_successor(session, principal, anchor_work, context, anchor, body):
    """Return additive metadata for the existing own_sent_receipt response."""
    from src.crm.services import followup_service as service

    known = deepcopy(anchor_work.payload.get('followup_anchor_v1') or {})
    own = known.get('decision') or {}
    if (principal.workspace_id != WORKSPACE_ID or anchor_work.lead_id != LEAD_ID
            or anchor['id'] != MESSAGE_ID or own.get('source_work_id') != str(SOURCE_WORK_ID)):
        return {}  # This patch has no authority over any other historical send.
    require('work:write' in principal.scopes, 'write_scope_required')
    service.mailbox_scope(principal, MAILBOX)
    require(session.in_transaction(), 'reconciliation_transaction_required')
    require(anchor_work.workspace_id == WORKSPACE_ID and anchor_work.kind == 'sent_review'
            and anchor_work.status == 'completed' and own.get('purpose') == 'own_sent_receipt'
            and known['classification'] == 'fu3' and known['mailbox'] == MAILBOX
            and known['provider_message_id'] == MESSAGE_ID
            and known['thread_id'] == THREAD_ID and known['lead_id'] == str(LEAD_ID),
            'own_receipt_identity_mismatch')
    _locked_company_lead(session, WORKSPACE_ID, LEAD_ID)
    # Reacquire NOWAIT rather than assume a caller's object is a locked fresh row.
    locked = _locked_rows(session, select(AgentWork).where(
        AgentWork.workspace_id == WORKSPACE_ID,
        AgentWork.id.in_([anchor_work.id, SOURCE_WORK_ID]),
    ).order_by(AgentWork.id))
    source = next((row for row in locked if row.id == SOURCE_WORK_ID), None)
    current_anchor = next((row for row in locked if row.id == anchor_work.id), None)
    require(source is not None and current_anchor is not None
            and digest(current_anchor.payload.get('followup_anchor_v1')) == digest(known),
            'historical_work_changed')
    coverage = context.get('coverage') or {}
    require(all(coverage.get(name) is True for name in (
        'complete', 'crm_complete', 'gmail_complete', 'direct_drafts_complete', 'related_threads_complete')),
        'incomplete_context')
    expected_hash = digest({'crm': context['crm'], 'provider': context['provider']})
    require(body.lead_id == LEAD_ID and body.mailbox == MAILBOX and body.anchor_message_id == MESSAGE_ID
            and context['crm']['lead']['id'] == str(LEAD_ID)
            and body.expected_lead_version == context['crm']['lead']['version']
            and body.context_hash == context['context_hash'] == coverage['coverage_id']
            == body.coverage_id == expected_hash,
            'context_changed')
    _lock_snapshot(session, WORKSPACE_ID, context)
    require(service.verified_anchor(context, MESSAGE_ID) == anchor
            and anchor['thread_id'] == THREAD_ID, 'own_receipt_changed')
    try:
        raw, obligation, intent, historical_decision = _historical_payload(source, known, anchor)
    except (KeyError, TypeError, AttributeError):
        raise FollowupError('historical_receipt_incomplete') from None
    matches = [message for message in service.messages(context)
               if receipt_matches(message, raw, obligation, timestamp(intent['reserved_at']))]
    require(len(matches) == 1 and matches[0] == anchor, 'sent_not_uniquely_verified')
    activities = list(session.scalars(select(Activity).join(
        SourceIdentity, Activity.source_identity_id == SourceIdentity.id).where(
        Activity.workspace_id == WORKSPACE_ID, Activity.lead_id == LEAD_ID,
        SourceIdentity.workspace_id == WORKSPACE_ID, SourceIdentity.source_system == 'gmail',
        SourceIdentity.source_scope == 'mailbox:' + MAILBOX,
        SourceIdentity.entity_kind == 'message', SourceIdentity.external_id == MESSAGE_ID,
    )))
    require(len(activities) == 1, 'receipt_identity_unavailable')
    activity = activities[0]
    task = session.get(Task, SOURCE_TASK_ID)
    require(task is not None and task.workspace_id == WORKSPACE_ID and task.lead_id == LEAD_ID
            and task.source_rule == POLICY_ID and task.task_type == 'email'
            and task.status == 'completed' and task.version == 2
            and activity.activity_type == 'email_sent' and activity.direction == 'outbound'
            and activity.occurred_at == timestamp(anchor['sent_at'])
            and (source.result or {}).get('activity_id') == str(activity.id)
            and (source.result or {}).get('provider_message_id') == MESSAGE_ID
            and (source.result or {}).get('intent_state') == 'crm_applied'
            and source.result_hash == digest(source.result), 'completed_predecessor_changed')
    require(task is not None and ((task.completion_activity_id == activity.id
             and task.completed_at == timestamp(anchor['sent_at']))
            or _verified_manual_completion(session, task, source)), 'completed_predecessor_changed')

    key = obligation_key(WORKSPACE_ID, MAILBOX, LEAD_ID, THREAD_ID, 4, MESSAGE_ID)
    identifier, next_work_id = task_id(WORKSPACE_ID, key), uuid5(WORKSPACE_ID, 'agent-work:' + key)
    existing_tasks = _locked_rows(session, select(Task).where(
        Task.workspace_id == WORKSPACE_ID, Task.id == identifier))
    existing_work = _locked_rows(session, select(AgentWork).where(
        AgentWork.workspace_id == WORKSPACE_ID, or_(
            AgentWork.id == next_work_id, AgentWork.source_key == key, AgentWork.task_id == identifier,
            (AgentWork.lead_id == LEAD_ID)
            & (AgentWork.payload['followup_v1']['obligation']['anchor_message_id'].astext == MESSAGE_ID)
            & (AgentWork.payload['followup_v1']['obligation']['fu_stage'].astext == '4'),
        )))
    expected_link = {'task_id': str(identifier), 'work_id': str(next_work_id), 'obligation_key': key}
    if existing_tasks or existing_work:
        require(len(existing_tasks) == len(existing_work) == 1
                and existing_tasks[0].lead_id == LEAD_ID and existing_work[0].id == next_work_id
                and existing_work[0].source_key == key and existing_work[0].task_id == identifier
                and existing_work[0].lead_id == LEAD_ID and existing_work[0].kind == 'followup_due',
                'successor_identity_conflict')
        # Never reschedule, requeue, recreate or overwrite an existing edited,
        # cancelled, completed or reviewed successor. Its current state wins.
        return {'next_task_id': str(identifier), 'next_work_id': str(next_work_id),
                'successor_reconciliation': 'already_exists'}
    require(not known.get('obligation') and not (source.result or {}).get('next_task_id'),
            'successor_pointer_conflict')
    fresh_assessment = service.assessment(context, anchor)
    if fresh_assessment['disposition'] != 'eligible':
        return {'next_task_id': None, 'successor_reconciliation': 'blocked',
                'successor_assessment': fresh_assessment}
    # Canonical deterministic identity, cadence, not_before and normal queue.
    successor = service.create_obligation(session, WORKSPACE_ID, task.owner_user_id, context, anchor, 4)
    require(successor == expected_link, 'successor_identity_conflict')
    details = {'policy': POLICY, 'source_work_id': str(source.id), 'intent_id': intent['intent_id'],
               'review_request_id': historical_decision['review_request_id'],
               'reviewer_receipt_id': historical_decision['reviewer_receipt_id'],
               'provider_message_id': MESSAGE_ID, 'observation_version': digest(anchor),
               'context_hash': context['context_hash'], 'coverage_id': coverage['coverage_id'],
               'lead_version': body.expected_lead_version, 'completed_task_version': task.version,
               'successor': successor, 'assessment': fresh_assessment,
               'applied_at': utc(datetime.now(UTC))}
    known['obligation'] = successor
    known['successor_reconciliation_v1'] = details
    save_ledger(current_anchor, known)
    audit(session, current_anchor, principal.actor_id, 'missing_successor_reconciled', details)
    session.flush()
    return {'next_task_id': successor['task_id'], 'next_work_id': successor['work_id'],
            'successor_reconciliation': 'created'}
