"""Read-only historical inventory + resumable dry-run. NOT lead hygiene.

All note/call rows and all leads in one tenant are accounted for, including empty,
imported, superseded and unscoped sources. A replay never applies CRM projections.
Only explicit simulations in a disposable local DB may create decision receipts.
"""
from collections import Counter
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from sqlalchemy import select, text

from src.crm.persistence.models import Activity, AgentWork, Lead
from src.crm.services.jev_note_policy import catalog, digest, build_note_request
from src.crm.services.jev_note_worker import (
    KIND, load_snapshot, process_snapshot, preview_hints, require_simulation_database,
)
from src.crm.services.note_source_service import source_digest
from src.crm.services.jev_worker import JevHTTP, MAX_REQUEST_BYTES


@contextmanager
def _reader(factory):
    with factory() as session:
        session.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
        session.execute(text('SET TRANSACTION READ ONLY'))
        yield session


def create_plan(factory, workspace_id, *, flags=None):
    """Stable membership/snapshot manifest, no raw text and no DB writes."""
    selected = dict.fromkeys(catalog()['points'], 'off') | (flags or {})
    if set(selected) != set(catalog()['points']) or any(m not in ('off', 'shadow', 'canary') for m in selected.values()):
        raise ValueError('flags_invalid')
    with _reader(factory) as session:
        leads = list(session.scalars(select(Lead).where(Lead.workspace_id == workspace_id).order_by(Lead.id)))
        sources = list(session.scalars(select(Activity).where(Activity.workspace_id == workspace_id,
            Activity.activity_type.in_(('note', 'call'))).order_by(Activity.created_at, Activity.id)))
        superseded = {row.supersedes_activity_id for row in session.scalars(select(Activity).where(
            Activity.workspace_id == workspace_id, Activity.supersedes_activity_id.is_not(None)))}
        items = []
        for source in sources:
            item = {'activity_id': str(source.id), 'lead_id': str(source.lead_id) if source.lead_id else None,
                'source_digest': source_digest(source), 'source_origin': source.actor_type,
                'eligibility': 'ready', 'snapshot_digest': None}
            if source.id in superseded:
                item['eligibility'] = 'superseded'
            elif source.lead_id is None:
                item['eligibility'] = 'unscoped'
            elif not (source.summary or '').strip():
                item['eligibility'] = 'empty_note'
            else:
                snapshot = load_snapshot(session, workspace_id, source.lead_id, item)
                item['snapshot_digest'] = digest(snapshot)
                try:
                    request = build_note_request(snapshot, flags=selected)
                    if len(json.dumps(request['body'], allow_nan=False, separators=(',', ':')).encode()) > MAX_REQUEST_BYTES:
                        raise ValueError('request_limit')
                except (ValueError, TypeError, KeyError):
                    # A bad/oversized source is visible, never silently dropped
                    # or allowed to abort the remaining historical inventory.
                    item['eligibility'] = 'request_rejected'
            items.append(item)
        counts = Counter(item['lead_id'] for item in items)
        plan = {'schema': 'crm-note-replay/v1', 'workspace_id': str(workspace_id),
            'catalog_digest': digest(catalog()), 'flags': selected, 'items': items,
            'leads': [{'lead_id': str(lead.id), 'source_count': counts[str(lead.id)]} for lead in leads],
            'coverage': 'all_note_call_rows_and_all_leads_no_hygiene_filters',
            'scope_limits': 'Emails/meetings are context; unscoped notes require explicit association, not guessed.'}
        return dict(plan, plan_id=digest(plan))


def _validate(plan, workspace_id):
    if (plan.get('workspace_id') != str(workspace_id)
            or plan.get('catalog_digest') != digest(catalog())
            or plan.get('plan_id') != digest({k: v for k, v in plan.items() if k != 'plan_id'})):
        raise ValueError('plan_tenant_policy_or_digest_changed')


def _current(factory, workspace_id, item):
    if item['eligibility'] != 'ready':
        return None
    try:
        with _reader(factory) as session:
            snapshot = load_snapshot(session, workspace_id, UUID(item['lead_id']), item)
        return snapshot if digest(snapshot) == item['snapshot_digest'] else None
    except ValueError:
        return None


def _open_checkpoint(path, plan, mode):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('checkpoint_symlink')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), plan_id TEXT, mode TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS progress (position INTEGER PRIMARY KEY, result TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO identity VALUES (1,?,?)', (plan['plan_id'], mode))
        if db.execute('SELECT plan_id, mode FROM identity').fetchone() != (plan['plan_id'], mode):
            raise ValueError('checkpoint_plan_or_mode_changed')
        db.commit()
        return db
    except Exception:
        db.close()
        raise


def _checkpoint_result(index, encoded, plan):
    result = json.loads(encoded)
    checksum = result.pop('checkpoint_digest', None)
    if (type(index) is not int or not 0 <= index < len(plan['items'])
            or result.get('activity_id') != plan['items'][index]['activity_id']
            or checksum != digest({'plan': plan['plan_id'], 'index': index, 'result': result})):
        raise ValueError('checkpoint_result_changed')
    return result


async def replay_batch(factory, workspace_id, plan, checkpoint, *, limit=50, http=None, simulate=False):
    """Default: read-only DB/no provider. Resume is identity-bound, not timestamps.

    Crash after provider receipt but before local checkpoint resolves to the
    shared SQL receipt on resume. Pending/uncertain submissions are never retried.
    Every report revalidates already-processed snapshots to flag stale results.
    """
    _validate(plan, workspace_id)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('batch_limit')
    if simulate:
        if not isinstance(http, JevHTTP) or not http.simulated:
            raise ValueError('mock_required')
        with factory() as session:
            require_simulation_database(session)
    with closing(_open_checkpoint(checkpoint, plan, 'simulation' if simulate else 'dry_run')) as db:
        for index, encoded in db.execute('SELECT position, result FROM progress'):
            _checkpoint_result(index, encoded, plan)
        for _ in range(limit):
            db.execute('BEGIN IMMEDIATE')
            completed = {row[0] for row in db.execute('SELECT position FROM progress')}
            index = next((i for i in range(len(plan['items'])) if i not in completed), None)
            if index is None:
                db.commit()
                break
            item = plan['items'][index]
            result = {'activity_id': item['activity_id'], 'status': item['eligibility'], 'receipt_id': None,
                      'diff': [], 'current': None}
            if item['eligibility'] == 'ready':
                snapshot = _current(factory, workspace_id, item)
                if snapshot is None:
                    result['status'] = 'snapshot_changed'
                elif simulate:
                    receipt = await process_snapshot(factory, snapshot, http=http, flags=plan['flags'])
                    result.update(status=receipt['status'], receipt_id=receipt.get('receipt_id'))
                    if receipt.get('receipt_id'):
                        proposed = preview_hints(factory, workspace_id, receipt['receipt_id'])
                        result.update(diff=proposed.get('diff', []), diff_digest=proposed.get('diff_digest'))
                else:
                    request = build_note_request(snapshot, flags=plan['flags'])
                    result['status'] = 'disabled' if not request['body']['questions'] else 'needs_evaluation'
                    with _reader(factory) as session:
                        prior = session.scalar(select(AgentWork).where(AgentWork.workspace_id == workspace_id,
                            AgentWork.kind == KIND, AgentWork.source_key == 'jev-note:' + request['decision_id']))
                        if prior:
                            result.update(status=prior.result['status'], receipt_id=str(prior.id))
                    if result['receipt_id']:
                        proposed = preview_hints(factory, workspace_id, result['receipt_id'])
                        result.update(diff=proposed.get('diff', []), diff_digest=proposed.get('diff_digest'))
            result['checkpoint_digest'] = digest({'plan': plan['plan_id'], 'index': index, 'result': result})
            db.execute('INSERT INTO progress VALUES (?,?)', (index, json.dumps(result, allow_nan=False)))
            db.commit()
        results = []
        points = {key: {'flag': plan['flags'][key], 'evaluated': 0, 'abstained': 0,
            'human_conflicts': 0, 'quality_measured': False, 'false_negative_rate': None,
            'provider_cost_eur': None, 'provider_latency_ms': None} for key in catalog()['points']}
        for index, encoded in db.execute('SELECT position, result FROM progress ORDER BY position'):
            result = _checkpoint_result(index, encoded, plan)
            if plan['items'][index]['eligibility'] == 'ready':
                snapshot = _current(factory, workspace_id, plan['items'][index])
                result['current'] = snapshot is not None
                # Checkpoints are cursors, not authority. Always read exact SQL
                # receipt and fresh preview; stale hints are never exportable.
                result.update(diff=[], decisions={})
                result.pop('diff_digest', None)
                if snapshot is not None and result['receipt_id']:
                    request = build_note_request(snapshot, flags=plan['flags'])
                    with _reader(factory) as session:
                        row = session.scalar(select(AgentWork).where(
                            AgentWork.workspace_id == workspace_id, AgentWork.kind == KIND,
                            AgentWork.id == UUID(result['receipt_id']),
                            AgentWork.source_key == 'jev-note:' + request['decision_id']))
                        if row is None:
                            raise ValueError('checkpoint_receipt_unavailable')
                        result['status'] = row.result['status']
                        if row.result['status'] == 'succeeded':
                            result['decisions'] = row.result['decisions']
                    proposed = preview_hints(factory, workspace_id, result['receipt_id'])
                    if proposed['status'] != 'conflict':
                        result.update(diff=proposed['diff'], diff_digest=proposed['diff_digest'])
                    else:
                        result.update(current=False, decisions={})
                for key, decision in result['decisions'].items():
                    points[key]['evaluated'] += 1
                    points[key]['abstained'] += decision['value'] == 'unknown'
                    points[key]['human_conflicts'] += decision['reason'] == 'human_conflict'
            results.append(result)
    return {'plan_id': plan['plan_id'], 'mode': 'simulation' if simulate else 'dry_run',
        'total': len(plan['items']), 'completed': len(results), 'has_more': len(results) < len(plan['items']),
        'leads': len(plan['leads']), 'leads_without_notes': sum(x['source_count'] == 0 for x in plan['leads']),
        'results': results, 'status_counts': dict(Counter(r['status'] for r in results)),
        'paid_calls': 0, 'crm_writes': 0, 'receipt_writes_possible': simulate,
        'points': points, 'quality_measured': False, 'provider_cost_eur': None,
        'automatic_apply': False, 'scope': plan['coverage']}
