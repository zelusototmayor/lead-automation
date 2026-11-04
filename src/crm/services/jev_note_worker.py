"""Post-commit CRM note classifier, OFF by default; no Sales lease or fact writes.

The existing call_followup outbox is read, never claimed/finished here. Dedicated
AgentWork(kind=jev_note_decision) rows hold shared-PG receipts; waiting rows cannot
be picked by the normal queue. No migration, timer, mail or Calendar connector.
Operational transport requires explicit target/tenant scope and a PG budget
suballocation. Existing LivePermit strings alone cannot enable this surface.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import os
import time
from uuid import UUID, uuid4, uuid5

import httpx
from sqlalchemy import select, or_, func, exists
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from src.crm.persistence.models import Activity, Account, AgentWork, AuditEvent, Contact, Lead
from src.crm.services.note_source_service import note_context, source_digest, source_snapshot
from src.crm.services.jev_note_policy import build_note_request, digest, map_answers
from src.crm.services import jev_call_facts, jose_note_policy, jose_note_units
from src.crm.services.jev_worker import JevHTTP, MAX_REQUEST_BYTES

KIND = 'jev_note_decision'

_FAILURE_CODES = frozenset({'encoded_response_rejected', 'response_limit', 'request_limit',
    'model_version_mismatch', 'answer_shape_invalid', 'answer_invalid', 'choice_not_maximum',
    'usage_invalid', 'usage_exceeds_reserved_contract'})


def _failure_detail(error):
    """Finite, non-prose categories only. Never stringify a transport exception."""
    if isinstance(error, httpx.HTTPStatusError):
        return {'kind': 'http_status', 'http_status': error.response.status_code}
    if isinstance(error, json.JSONDecodeError):
        return {'kind': 'invalid_json'}
    if type(error) is ValueError and error.args and isinstance(error.args[0], str) and error.args[0] in _FAILURE_CODES:
        return {'kind': 'validation', 'code': error.args[0]}
    return {'kind': 'invalid_response'}



def _build_request(snapshot, flags):
    if flags == jose_note_units.FLAGS:
        return jose_note_units.build_request(snapshot, flags=flags)
    if flags == jose_note_policy.FLAGS:
        return jose_note_policy.build_request(snapshot, flags=flags)
    if jev_call_facts.is_factual(flags):
        return jev_call_facts.build_request(snapshot, flags=flags)
    return build_note_request(snapshot, flags=flags)


def require_simulation_database(session):
    """Fail closed before any receipt/hint write; no libpq redirect channels."""
    url = session.get_bind().url
    if (os.environ.get('CRM_DISPOSABLE_TEST_DATABASE') != '1'
            or url.drivername != 'postgresql+psycopg'
            or url.host not in ('127.0.0.1', 'localhost', '::1')
            or not url.database or 'test' not in url.database.lower() or url.query
            or any(os.environ.get(k) for k in (
                'PGDATABASE', 'PGHOST', 'PGHOSTADDR', 'PGPASSFILE', 'PGPASSWORD',
                'PGPORT', 'PGSERVICE', 'PGSERVICEFILE', 'PGUSER'))):
        raise ValueError('disposable_database_required')


def load_snapshot(session, workspace_id, lead_id, payload, *, flags=None):
    lead = session.scalar(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id == lead_id))
    if lead is None:
        raise ValueError('lead_unavailable')
    source = note_context(session, workspace_id, lead_id, payload)
    if source.get('status') != 'current':
        raise ValueError('source_not_current')
    # Source filtering is deliberately NOT hygiene/suppression: historical lost,
    # imported, no-answer and incomplete leads must remain visible in dry-run.
    scope = or_(Activity.lead_id == lead_id, Activity.account_id == lead.account_id) if lead.account_id else Activity.lead_id == lead_id
    rows = list(session.scalars(select(Activity).where(Activity.workspace_id == workspace_id, scope)
                               .order_by(Activity.id)))
    revisions = [(str(row.id), source_digest(row)) for row in rows]
    current = next(row for row in rows if str(row.id) == source['activity_id'])
    if flags in (jose_note_policy.FLAGS, jose_note_units.FLAGS):
        source['provenance'] = {'actor_type': current.actor_type, 'source_system': current.source_system,
            'title': current.title, 'actor_id': str(current.actor_id) if current.actor_id else None,
            'source_identity_id': str(current.source_identity_id) if current.source_identity_id else None,
            'ingest_event_id': str(current.ingest_event_id) if current.ingest_event_id else None}
    # This richer context belongs to the Jev lane, not the shared Sales seam.
    # Exclude superseded sources before limiting; retain explicit incompleteness.
    replacement = aliased(Activity)
    active = select(Activity).where(Activity.workspace_id == workspace_id, scope,
        Activity.id != current.id,
        Activity.activity_type.in_(('note', 'call', 'email_sent', 'email_received', 'meeting')),
        ~exists(select(replacement.id).where(replacement.workspace_id == workspace_id,
            replacement.supersedes_activity_id == Activity.id)))
    history = list(session.scalars(active.where(Activity.occurred_at < current.occurred_at)
        .order_by(Activity.occurred_at.desc(), Activity.id).limit(101)))
    recent = list(session.scalars(active.where(or_(Activity.created_at >= current.created_at,
        Activity.occurred_at >= current.occurred_at))
        .order_by(Activity.created_at.desc(), Activity.id).limit(31)))
    source['history'] = {'items': [source_snapshot(row) for row in history[:100]],
        'has_more': len(history) > 100, 'prior_contact_known': bool(history),
        'all_channel_history_complete': False}
    source['newer_context'] = {'items': [source_snapshot(row) for row in recent[:30]],
        'has_more': len(recent) > 30}
    account = session.scalar(select(Account).where(Account.workspace_id == workspace_id,
        Account.id == lead.account_id)) if lead.account_id else None
    contact = session.scalar(select(Contact).where(Contact.workspace_id == workspace_id,
        Contact.id == lead.contact_id)) if lead.contact_id else None
    return {'workspace_id': str(workspace_id), 'lead_id': str(lead_id),
        'lead_version': lead.version, 'lead_stage': lead.stage, 'lead_priority': lead.priority,
        'account_id': str(lead.account_id) if lead.account_id else None,
        'account_version': account.version if account else None,
        'contact_id': str(lead.contact_id) if lead.contact_id else None,
        'contact_version': contact.version if contact else None,
        'context_revision': digest(revisions), 'source': source}


def _receipt(request, *, simulated=True):
    return {k: deepcopy(v) for k, v in request.items() if k not in ('body','unit_requests')} | {
        'receipt_id': str(uuid5(UUID(request['workspace_id']), 'jev-note:' + request['decision_id'])),
        'status': 'pending', 'decisions': {}, 'fact_authority': False, 'simulated': simulated,
        'usage': None, 'actual_cost_eur': None,
        'cost_status': 'simulated_not_billed' if simulated else 'unreconciled',
        'latency_ms': None, 'error': None, 'application': {'status': 'not_applied'}}


def _expire(session, row):
    result = deepcopy(row.result)
    if result['status'] == 'pending' and row.available_at <= session.scalar(select(func.now())):
        result.update(status='uncertain', error='deadline_or_worker_lost', decisions={})
        row.result, row.status = result, 'completed'
        row.result_hash = digest(result)
    return result


def claim_receipt(factory, request, *, deadline_seconds, runtime=None, simulated=True):
    """Unique shared SQL intent committed before HTTP; no automatic resend."""
    receipt = _receipt(request, simulated=simulated)
    source_key = 'jev-note:' + request['decision_id']
    if runtime is not None:
        # Operational intent is immutable even when the decision snapshot drifts.
        # Keep snapshot identity separately for freshness and simulation replay.
        source_key = 'jev-note-work:' + digest({'target': runtime.target, 'work_id': str(runtime.work_id)})
        if runtime.policy in ('personal_notes','personal_notes_v2'):
            source_key = 'jev-personal-note:' + digest({'target': runtime.target,
                'event_id': request['event_id'], 'version': request['version']})
        receipt.update(receipt_id=str(uuid5(UUID(request['workspace_id']), source_key)),
                       work_id=str(runtime.work_id))
    receipt_id, owner = UUID(receipt['receipt_id']), uuid4().hex
    with factory.begin() as session:
        budget = None
        if runtime is None:
            require_simulation_database(session)
        else:
            from src.crm.services.jev_note_runtime import RESERVATION_MICROEUR
            runtime.validate(session, UUID(request['workspace_id']))
            budgets = runtime.budgets(session)
            work = session.scalar(select(AgentWork).where(AgentWork.workspace_id == runtime.workspace_id,
                AgentWork.id == runtime.work_id, AgentWork.kind == 'call_followup'))
            if (simulated or work is None or work.lead_id != UUID(request['lead_id'])
                    or work.payload.get('activity_id') != request['event_id']):
                raise ValueError('work_scope_invalid')
            runtime.validate_source(session,work)
            # Also recognize pre-work-identity operational receipts. Never infer
            # permission to resend just because an older receipt lacks work_id.
            priors = list(session.scalars(select(AgentWork).where(
                AgentWork.workspace_id == runtime.workspace_id, AgentWork.kind == KIND,
                or_(AgentWork.id == receipt_id,
                    (AgentWork.result['target'].astext == runtime.target)
                    & (AgentWork.result['simulated'].astext == 'false')
                    & or_(AgentWork.result['work_id'].astext == str(runtime.work_id),
                           AgentWork.payload['event_id'].astext == request['event_id']))
                ).where(AgentWork.result['version'].astext == request['version']
                    if runtime.policy in ('personal_notes','personal_notes_v2')
                    else AgentWork.result['version'].astext.notin_((jose_note_policy.VERSION,jose_note_units.VERSION))).with_for_update()))
            if runtime.retry is not None:
                from src.crm.services.jev_retry import claim_retry
                return claim_retry(session, request, runtime, work, priors, receipt,
                                   owner, deadline_seconds, budgets)
            if len(priors) > 1:
                from src.crm.services.jev_retry import replay_row
                priors = [replay_row(session, priors)]
            elif priors and ('retry' in (priors[0].result or {}) or priors[0].source_key.startswith('jev-note-retry/')):
                raise ValueError('incomplete_retry_chain')
            if priors:
                prior = priors[0]
                imported = (runtime.policy in ('personal_notes','personal_notes_v2')
                    and isinstance(prior.result.get('offline_import'), dict)
                    and prior.result_hash == digest(prior.result)
                    and prior.result.get('reserved_microeur') == 0)
                if (prior.result.get('target') != runtime.target or prior.result.get('simulated') is not False
                        or (not imported and not runtime.accepts_ref(prior.result.get('budget_ref')))
                        or prior.result.get('work_id') not in (None, str(runtime.work_id))
                        or prior.lead_id != work.lead_id or prior.payload.get('event_id') != request['event_id']):
                    raise ValueError('receipt_binding_changed')
                if imported:
                    _current_request(session, prior)
                if prior.result.get('work_id') is None:
                    prior.result = dict(prior.result, work_id=str(runtime.work_id))
                    prior.result_hash = digest(prior.result)
                return None, _expire(session, prior)
            budget = next((b for b in budgets if b.result['reserved_microeur'] + RESERVATION_MICROEUR
                           <= b.payload['cap_microeur']), None)
            if budget is None:
                raise ValueError('budget_exhausted')
            reserved = budget.result['reserved_microeur']
            receipt.update(target=runtime.target, budget_ref=budget.payload['budget_ref'],
                           reserved_microeur=RESERVATION_MICROEUR)
        now = session.scalar(select(func.now()))
        inserted = session.execute(insert(AgentWork).values(
            id=receipt_id, workspace_id=UUID(request['workspace_id']), lead_id=UUID(request['lead_id']),
            source_key=source_key, kind=KIND, status='waiting',
            worker_id=owner, attempts=1, available_at=now + timedelta(seconds=deadline_seconds),
            payload={k: request[k] for k in ('event_id', 'source_digest', 'snapshot_digest', 'flags')},
            result=receipt).on_conflict_do_nothing(constraint='uq_agent_work_source').returning(AgentWork.id)).scalar()
        if inserted:
            if budget is not None:
                budget.result = {'reserved_microeur': reserved + RESERVATION_MICROEUR}
            return owner, receipt
        row = session.scalar(select(AgentWork).where(AgentWork.workspace_id == UUID(request['workspace_id']),
            AgentWork.id == receipt_id, AgentWork.kind == KIND).with_for_update())
        if row is None:
            raise ValueError('receipt_collision')
        return None, _expire(session, row)


def finish_receipt(factory, workspace_id, owner, result, *, runtime=None):
    with factory.begin() as session:
        if runtime is None:
            require_simulation_database(session)
        else:
            runtime.validate(session, workspace_id)
        row = session.scalar(select(AgentWork).where(AgentWork.workspace_id == workspace_id,
            AgentWork.id == UUID(result['receipt_id']), AgentWork.kind == KIND).with_for_update())
        if row is None:
            raise ValueError('receipt_missing')
        current = _expire(session, row)
        if row.worker_id != owner or current['status'] != 'pending':
            return current
        row.result, row.status = deepcopy(result), 'completed'
        row.result_hash = digest(result)
        return deepcopy(result)


async def process_snapshot(factory, snapshot, *, http, flags=None, runtime=None):
    """Separate worker only. Snapshot revalidated before hints are ever used."""
    try:
        request = _build_request(snapshot, flags)
    except (ValueError, TypeError, KeyError):
        return {'status': 'fallback', 'error': 'invalid_snapshot', 'decisions': {}}
    if not request['body']['questions']:
        return {'status': 'disabled', 'decisions': {}}
    if not isinstance(http, JevHTTP) or (runtime is None and not http.simulated):
        return {'status': 'fallback', 'error': 'live_note_privacy_budget_gate', 'decisions': {}}
    try:
        http.preflight()
        with factory() as session:
            if runtime is None:
                require_simulation_database(session)
            else:
                runtime.validate(session, UUID(request['workspace_id']))
                allowed = (jose_note_units.FLAGS if runtime.policy == 'personal_notes_v2' else
                    jose_note_policy.FLAGS if runtime.policy == 'personal_notes' else
                    jev_call_facts.FLAGS if runtime.policy == 'call_facts' else {'interest': 'canary'})
                if http.simulated or flags != allowed:
                    raise ValueError('operational_transport_or_scope_invalid')
                work = session.scalar(select(AgentWork).where(AgentWork.workspace_id == runtime.workspace_id,
                    AgentWork.id == runtime.work_id, AgentWork.kind == 'call_followup'))
                if work is None or work.lead_id != UUID(request['lead_id']) or work.payload.get('activity_id') != request['event_id']:
                    raise ValueError('work_scope_invalid')
            fresh = load_snapshot(session, UUID(request['workspace_id']), UUID(request['lead_id']), {
                'activity_id': request['event_id'], 'source_digest': request['source_digest']}, flags=flags)
            if _build_request(fresh, flags)['snapshot_digest'] != request['snapshot_digest']:
                raise ValueError('snapshot_changed')
        if len(json.dumps(request['body'], allow_nan=False, separators=(',', ':')).encode()) > MAX_REQUEST_BYTES:
            raise ValueError('request_limit')
        owner, receipt = claim_receipt(factory, request, deadline_seconds=http.deadline_seconds,
                                       runtime=runtime, simulated=http.simulated)
    except (SQLAlchemyError, ValueError):
        return {'status': 'fallback', 'error': 'receipt_or_preflight_unavailable', 'decisions': {}}
    if owner is None:
        if runtime is not None and receipt['decision_id'] != request['decision_id']:
            # Preserve stored audit, but never present an old application as current.
            return dict(receipt, decisions={}, application={'status': 'conflict', 'error': 'stale_or_unavailable'})
        return receipt
    started = time.monotonic()
    try:
        response = await http.evaluate(request['body'])
        decisions = (jose_note_units.map_response(request, response) if flags == jose_note_units.FLAGS else
                     jose_note_policy.map_response(request, response) if flags == jose_note_policy.FLAGS else
                     jev_call_facts.map_response(request, response)
                     if jev_call_facts.is_factual(flags) else map_answers(request, response))
        usage = response.get('usage')
        if (not isinstance(usage, dict) or set(usage) != {'input_tokens', 'output_tokens'}
                or any(type(v) is not int or not 0 <= v <= 1_000_000_000 for v in usage.values())):
            raise ValueError('usage_invalid')
        if runtime is not None and usage['input_tokens'] > 64_000:
            raise ValueError('usage_exceeds_reserved_contract')
        receipt.update(status='succeeded', decisions=decisions, usage=usage)
    except asyncio.CancelledError:
        receipt.update(status='uncertain', error='cancelled')
        try:
            finish_receipt(factory, UUID(request['workspace_id']), owner, receipt, runtime=runtime)
        except (SQLAlchemyError, ValueError):
            pass
        raise
    except (TimeoutError, httpx.RequestError):
        receipt.update(status='uncertain', error='transport_outcome_unknown')
    except (ValueError, TypeError, httpx.HTTPStatusError) as error:
        receipt.update(status='failed', error='response_invalid', failure_detail=_failure_detail(error))
    except Exception:
        receipt.update(status='uncertain', error='worker_outcome_unknown')
    receipt['latency_ms'] = round((time.monotonic() - started) * 1000)
    try:
        return finish_receipt(factory, UUID(request['workspace_id']), owner, receipt, runtime=runtime)
    except (SQLAlchemyError, ValueError):
        return dict(receipt, status='uncertain', error='receipt_commit_unknown', decisions={})


async def process_work(factory, workspace_id, work_id, *, http, flags=None, runtime=None):
    """Read canonical outbox from a NEW committed session; never delay log-call."""
    if not flags or all(mode == 'off' for mode in flags.values()):
        return {'status': 'disabled', 'decisions': {}}
    try:
        with factory() as session:
            if runtime is not None:
                runtime.validate(session, workspace_id)
                if work_id != runtime.work_id:
                    raise ValueError('work_scope_invalid')
            work = session.scalar(select(AgentWork).where(AgentWork.workspace_id == workspace_id,
                AgentWork.id == work_id, AgentWork.kind == 'call_followup'))
            if work is None or work.lead_id is None:
                raise ValueError('outbox_unavailable')
            if runtime is not None:
                runtime.validate_source(session,work)
            snapshot = load_snapshot(session, workspace_id, work.lead_id, work.payload, flags=flags)
    except (ValueError, SQLAlchemyError):
        return {'status': 'fallback', 'error': 'source_or_outbox_unavailable', 'decisions': {}}
    return await process_snapshot(factory, snapshot, http=http, flags=flags, runtime=runtime)


def _decision_row(session, workspace_id, receipt_id, *, lock=False):
    query = select(AgentWork).where(AgentWork.workspace_id == workspace_id,
        AgentWork.id == UUID(receipt_id), AgentWork.kind == KIND)
    row = session.scalar(query.with_for_update(nowait=True) if lock else query)
    if row is None:
        raise ValueError('receipt_unavailable')
    return row


def _current_request(session, row):
    from src.crm.services.jev_retry import validate_retry_receipt
    validate_retry_receipt(session, row)
    snapshot = load_snapshot(session, row.workspace_id, row.lead_id, {
        'activity_id': row.payload['event_id'], 'source_digest': row.payload['source_digest']}, flags=row.payload['flags'])
    request = _build_request(snapshot, row.payload['flags'])
    if request['decision_id'] != row.result.get('decision_id') or row.result.get('status') != 'succeeded':
        raise ValueError('stale_or_unsuccessful_decision')
    return request


def _applied_rows(session, workspace_id, lead_id, activity_id):
    return session.scalars(select(AgentWork).where(AgentWork.workspace_id == workspace_id,
        AgentWork.lead_id == lead_id, AgentWork.kind == KIND,
        AgentWork.payload['event_id'].astext == str(activity_id),
        AgentWork.result['application']['status'].astext == 'applied')
        .order_by(AgentWork.updated_at.desc(), AgentWork.id))


def _projection(session, workspace_id, lead_id, activity_id):
    hints, receipts = {}, {}
    for prior in _applied_rows(session, workspace_id, lead_id, activity_id):
        if jev_call_facts.is_factual(prior.payload.get('flags')):
            continue  # Factual interpretations are read via the canonical facts API.
        try:
            _current_request(session, prior)
        except ValueError:
            continue
        for key, hint in prior.result['application']['hints'].items():
            if key not in hints:
                hints[key], receipts[key] = deepcopy(hint), str(prior.id)
    return hints, receipts


def _preview(session, row):
    if jev_call_facts.is_factual(row.payload.get('flags')):
        raise ValueError('facts_require_canonical_writer')
    _current_request(session, row)
    if row.result['application']['status'] == 'applied':
        return deepcopy(row.result['application'])
    before, lineage = _projection(session, row.workspace_id, row.lead_id, row.payload['event_id'])
    hints = {k: d for k, d in row.result['decisions'].items() if row.payload['flags'][k] == 'canary'}
    diff = [{'point': key, 'before': before.get(key, {}).get('value'),
             'after': hints[key]['value']} for key in sorted(hints)
            if before.get(key, {}).get('value') != hints.get(key, {}).get('value')]
    supersedes = {key: lineage[key] for key in hints if key in lineage}
    return {'status': 'proposed', 'diff': diff,
        'diff_digest': digest({'receipt': str(row.id), 'snapshot': row.result['snapshot_digest'],
                              'supersedes': supersedes, 'diff': diff}),
        'hints': hints, 'supersedes_by_point': supersedes,
        'sales_context': {'owner': 'head-of-sales', 'source_work_unchanged': True,
            'allowed_actions': ['crm_context', 'review'], 'mail_authorized': False,
            'calendar_authorized': False, 'kpi_authorized': False}}


def preview_hints(factory, workspace_id, receipt_id):
    """Read-only diff required BEFORE the separately fenced CRM sidecar write."""
    try:
        with factory() as session:
            return _preview(session, _decision_row(session, workspace_id, receipt_id))
    except (SQLAlchemyError, ValueError, KeyError):
        return {'status': 'conflict', 'error': 'stale_or_unavailable', 'diff': []}


def apply_hints(factory, workspace_id, receipt_id, *, expected_diff, allow_simulated=False, runtime=None):
    """Advisory sidecar only, under explicit runtime or disposable simulation."""
    if runtime is None and allow_simulated is not True:
        return {'status': 'disabled'}
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    from src.crm.services.agent_work_service import WorkConflict
    try:
        with factory.begin() as session:
            if runtime is None:
                require_simulation_database(session)
            else:
                runtime.validate(session, workspace_id)
            row = _decision_row(session, workspace_id, receipt_id, lock=True)
            if runtime is not None:
                work = session.get(AgentWork, runtime.work_id)
                if (row.result.get('simulated') is not False or row.result.get('target') != runtime.target
                        or not runtime.accepts_ref(row.result.get('budget_ref')) or work is None
                        or work.workspace_id != workspace_id or work.lead_id != row.lead_id
                        or work.payload.get('activity_id') != row.payload['event_id']):
                    raise ValueError('runtime_receipt_mismatch')
            elif row.result.get('simulated') is not True:
                raise ValueError('simulation_required')
            lead, _ = _locked_company_lead(session, workspace_id, row.lead_id)
            if lead.contact_id:
                _locked_rows(session, select(Contact).where(Contact.workspace_id == workspace_id,
                                                           Contact.id == lead.contact_id))
            scope = or_(Activity.lead_id == row.lead_id, Activity.account_id == lead.account_id) if lead.account_id else Activity.lead_id == row.lead_id
            _locked_rows(session, select(Activity).where(Activity.workspace_id == workspace_id, scope))
            if runtime is not None:
                # Suppression and the source can change while inference runs.
                # Recheck after canonical entity locks and before any sidecar.
                runtime.validate_source(session, work)
            proposed = _preview(session, row)
            if proposed['diff_digest'] != expected_diff:
                raise ValueError('preview_changed')
            if proposed['status'] == 'applied':
                return proposed
            if not proposed['hints']:
                raise ValueError('no_canary_points')
            applied = dict(proposed, status='applied')
            row.result = dict(row.result, application=applied)
            row.updated_at = datetime.now(UTC)
            row.result_hash = digest(row.result)
            command = uuid5(row.id, 'hints-applied')
            session.add(AuditEvent(id=command, workspace_id=workspace_id,
                command_id=command, actor_id=workspace_id, action='jev.note_hints_applied',
                entity_type='activity', entity_id=UUID(row.payload['event_id']), details={
                    'receipt_id': str(row.id), 'snapshot_digest': row.result['snapshot_digest'],
                    'diff_digest': expected_diff, 'policy_version': row.result['version'],
                    'points': sorted(proposed['hints']), 'fact_authority': False,
                    'side_effects': 'advisory_sidecar_only', 'simulated': row.result['simulated']}))
            return applied
    except (SQLAlchemyError, ValueError, KeyError, WorkConflict):
        return {'status': 'conflict', 'error': 'stale_or_unavailable'}


def read_hints(factory, workspace_id, lead_id, activity_id):
    """Fresh authorized projection. Superseded/stale results remain audit only."""
    try:
        with factory() as session:
            return _projection(session, workspace_id, lead_id, activity_id)[0]
    except (SQLAlchemyError, ValueError, KeyError):
        return {}
