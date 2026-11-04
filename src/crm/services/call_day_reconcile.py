"""Explicit canonical CAS reconciliation, append-only history, no GET writes."""
from copy import deepcopy
import base64
import json
import zlib
from datetime import UTC, datetime
from uuid import UUID, uuid5
from fastapi import HTTPException
from sqlalchemy import select, or_, func
from src.crm.persistence.models import Activity, AuditEvent, CallDayPlan, Lead, Task
from src.crm.services.contact_channel import digest
from src.crm.services.note_source_service import source_digest

ACTION = 'pipeline.call_day_reconciled'


def _store_snapshot(session, principal, command_id, kind, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    encoded = base64.b64encode(zlib.compress(raw)).decode()
    parts = [encoded[i:i+2800] for i in range(0, len(encoded), 2800)]
    if len(raw) > 2_000_000 or len(parts) > 200:
        raise HTTPException(409, 'Plan snapshot exceeds bound')
    seed = uuid5(command_id, 'call-day-snapshot:' + kind)
    for index, part in enumerate(parts):
        key = uuid5(seed, str(index))
        session.add(AuditEvent(id=key, workspace_id=principal.workspace_id, command_id=key,
            actor_id=principal.actor_id, entity_type='call_day_snapshot', entity_id=command_id,
            action='pipeline.call_day_snapshot', details={'index':index, 'data':part}))
    return {'seed':str(seed), 'parts':len(parts), 'sha256':digest(payload)}


def _load_snapshot(session, workspace_id, ref):
    if not 1 <= ref['parts'] <= 200:
        raise HTTPException(409, 'Snapshot binding changed')
    parts = []
    for index in range(ref['parts']):
        key = uuid5(UUID(ref['seed']), str(index))
        row = session.get(AuditEvent, key)
        if (row is None or row.workspace_id != workspace_id or row.action != 'pipeline.call_day_snapshot'
                or row.details['index'] != index):
            raise HTTPException(409, 'Snapshot binding changed')
        parts.append(row.details['data'])
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(''.join(parts), validate=True), 2_000_001)
    if len(raw) > 2_000_000 or not decoder.eof or decoder.unused_data:
        raise HTTPException(409, 'Snapshot bound changed')
    payload = json.loads(raw)
    if digest(payload) != ref['sha256']:
        raise HTTPException(409, 'Snapshot hash changed')
    return payload


def plan_context_hash(session, workspace_id, payload):
    ids = [UUID(i) for i in payload.get('context_lead_ids', [i['lead_id'] for i in payload['items']])]
    leads = list(session.scalars(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(ids))))
    from src.crm.services.contact_channel import scope_ids_for_leads
    aliases = scope_ids_for_leads(session, workspace_id, leads)
    siblings = list(session.scalars(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(aliases))))
    scope = [l.id for l in siblings]
    acts = list(session.scalars(select(Activity).where(Activity.workspace_id == workspace_id, Activity.lead_id.in_(scope))))
    tasks = list(session.scalars(select(Task).where(Task.workspace_id == workspace_id, Task.lead_id.in_(scope))))
    return digest({'leads':sorted((str(l.id), l.version, str(l.account_id), l.stage) for l in siblings),
        'activities':sorted((str(a.id), source_digest(a), str(a.supersedes_activity_id)) for a in acts),
        'tasks':sorted((str(t.id), t.version, t.status, t.due_at, t.title, t.call_intent) for t in tasks)})


def _audits(session, workspace_id, day):
    key = uuid5(workspace_id, 'call-day:' + day.isoformat())
    return list(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id,
        AuditEvent.entity_type == 'call_day', AuditEvent.entity_id == key, AuditEvent.action == ACTION)))


def original_replay(session, principal, body, request_hash):
    for row in _audits(session, principal.workspace_id, body.work_date):
        before = _load_snapshot(session, principal.workspace_id, row.details['before_ref'])
        if (before['command_id'] == str(body.command_id) and row.details['before_request_hash'] == request_hash
                and row.details['before_actor_id'] == str(principal.actor_id)):
            return dict(before, replayed=True)
    return None


def prune_payload(before, current, command_id):
    """No replacement/recycling of already handled contacts to fill forty."""
    allowed = {i['lead_id']: i for i in current['items']}
    after = deepcopy(before)
    after['items'] = [allowed[i['lead_id']] for i in before['items'] if i['lead_id'] in allowed]
    after.update(total=len(after['items']), version=before['version'] + 1,
        context_lead_ids=before.get('context_lead_ids', [i['lead_id'] for i in before['items']]),
        command_id=str(command_id), generated_at=datetime.now(UTC).isoformat(),
        reconciliation={'previous_command_id':before['command_id'], 'previous_version':before['version'],
                        'removed_lead_ids':[i['lead_id'] for i in before['items'] if i['lead_id'] not in allowed],
                        'replacement_contacts_added':0},
        blockers=current['blockers'], excluded_candidates=current['excluded_candidates'])
    return after


def reconcile_call_day(session, principal, body):
    current = session.scalar(select(CallDayPlan).where(CallDayPlan.workspace_id == principal.workspace_id,
        CallDayPlan.work_date == body.work_date).with_for_update(nowait=True))
    material = body.model_dump(mode='json')
    if not body.replenish:
        material.pop('replenish', None)  # historical reconciliation replay
    semantic = digest(material)
    key = uuid5(principal.workspace_id, 'call-day:' + body.work_date.isoformat())
    audit_id = uuid5(body.command_id, ACTION)
    replay = session.get(AuditEvent, audit_id)
    if replay is not None:
        if (replay.workspace_id != principal.workspace_id or replay.entity_id != key or replay.action != ACTION
                or replay.actor_id != principal.actor_id or replay.details['request_hash'] != semantic):
            raise HTTPException(409, 'Command conflict')
        return dict(_load_snapshot(session, principal.workspace_id, replay.details['after_ref']), replayed=True)
    if current is None or current.version != body.expected_version:
        raise HTTPException(409, 'Plan changed')
    before = deepcopy(current.payload)
    # Full lead row fences FK-backed new notes/tasks; NOWAIT preserves human edits.
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    from src.crm.services.agent_work_service import WorkConflict
    try:
        if body.replenish:
            # Fence new candidate sources too, not only yesterday's short list.
            # NOWAIT fails rather than waiting on or overwriting human work.
            _locked_rows(session, select(Lead).where(Lead.workspace_id == principal.workspace_id).order_by(Lead.id))
            _locked_rows(session, select(Task).where(Task.workspace_id == principal.workspace_id).order_by(Task.id))
        for lead_id in sorted(set(before.get('context_lead_ids', [i['lead_id'] for i in before['items']]))):
            lead, _ = _locked_company_lead(session, principal.workspace_id, UUID(lead_id))
            from src.crm.services.contact_channel import scope_ids
            scope = scope_ids(session, principal.workspace_id, lead)
            _locked_rows(session, select(Lead).where(Lead.workspace_id == principal.workspace_id, Lead.id.in_(scope)))
            _locked_rows(session, select(Task).where(Task.workspace_id == principal.workspace_id, Task.lead_id.in_(scope)))
    except WorkConflict:
        raise HTTPException(409, 'Source context being edited') from None
    if plan_context_hash(session, principal.workspace_id, before) != body.context_sha256:
        raise HTTPException(409, 'Source context changed')
    if body.restore_command_id:
        matches = [_load_snapshot(session, principal.workspace_id, a.details['before_ref'])
                   for a in _audits(session, principal.workspace_id, body.work_date)
                   if a.details['before_command_id'] == str(body.restore_command_id)]
        if not matches:
            raise HTTPException(409, 'Rollback receipt unavailable')
        after = deepcopy(matches[0])
        after.update(version=current.version + 1, command_id=str(body.command_id),
                     restored_from_command_id=str(body.restore_command_id))
    else:
        from src.crm.services.call_day import build_call_day
        count = session.scalar(select(func.count()).select_from(Lead).where(Lead.workspace_id == principal.workspace_id))
        candidate = build_call_day(session, principal.workspace_id, body.work_date,
            before['capacity_minutes'], max(count, 1))
        after = (replenish_payload(before, candidate, body.command_id, body.target_contacts)
                 if body.replenish else prune_payload(before, candidate, body.command_id))
    before_ref = _store_snapshot(session, principal, body.command_id, 'before', before)
    after_ref = _store_snapshot(session, principal, body.command_id, 'after', after)
    session.add(AuditEvent(id=audit_id, workspace_id=principal.workspace_id, command_id=body.command_id,
        actor_id=principal.actor_id, entity_type='call_day', entity_id=key, action=ACTION,
        details={'request_hash':semantic, 'source_context_sha256':body.context_sha256,
                 'before_request_hash':current.request_hash, 'before_actor_id':str(current.actor_id),
                 'before_command_id':before['command_id'], 'before_ref':before_ref, 'after_ref':after_ref}))
    current.payload = after
    current.version = after['version']
    current.command_id = body.command_id
    current.actor_id = principal.actor_id
    current.request_hash = semantic
    session.flush()
    return dict(after, replayed=False)


def validate_justification(session, workspace_id, lead_id, reference, summary):
    """Recheck selection under source fences in the note's own transaction.

    Reading a latest lead version on the client is not selection-time CAS:
    a call on an account alias can arrive between plan generation and writing.
    Full lead/task NOWAIT fences also block FK-backed new source insertion.
    """
    from src.crm.persistence.models import Workspace, Account, Contact, SourceIdentity
    from src.crm.services.note_plan_service import _locked_rows
    from src.crm.services.agent_work_service import WorkConflict
    from src.crm.services.call_day import build_call_day
    try:
        command_id = UUID(reference.removeprefix('call-day/'))
        _locked_rows(session, select(Workspace).where(Workspace.id == workspace_id))
        plans = _locked_rows(session, select(CallDayPlan).where(CallDayPlan.workspace_id == workspace_id,
            CallDayPlan.command_id == command_id))
        if len(plans) != 1:
            raise HTTPException(409, 'Selection plan changed')
        plan = plans[0]
        leads = _locked_rows(session, select(Lead).where(Lead.workspace_id == workspace_id).order_by(Lead.id))
        _locked_rows(session, select(Task).where(Task.workspace_id == workspace_id).order_by(Task.id))
        # Existing source/identity edits do not necessarily insert an FK child.
        # Freeze those rows too; Workspace fences new workspace-backed rows.
        for model in (Activity, Account, Contact, SourceIdentity, AuditEvent):
            _locked_rows(session, select(model).where(model.workspace_id == workspace_id).order_by(model.id))
    except (WorkConflict, ValueError):
        raise HTTPException(409, 'Selection context being edited') from None
    prior = next((i for i in plan.payload['items'] if i['lead_id'] == str(lead_id)), None)
    candidate = build_call_day(session, workspace_id, plan.work_date,
        plan.payload['capacity_minutes'], max(len(leads), 1), enrich=False)
    current = next((i for i in candidate['items'] if i['lead_id'] == str(lead_id)), None)
    # Only current, source-backed selection may authorize a technical assertion.
    if not prior or not current or any(prior.get(k) != current.get(k)
            for k in ('reason', 'history_status', 'task', 'next_contact')):
        raise HTTPException(409, 'Selection source changed; reprepare')
    if summary == 'Justificação de lead: New pipeline — lead ainda não contactada.' and current.get('history_status') != 'confirmed_uncontacted':
        raise HTTPException(409, 'First-touch assertion no longer current')


def replenish_payload(before, current, command_id, target):
    """Revalidate current IDs; fill only the actual deficit. Audit keeps originals.

    Canonical selector already deduplicated alias/account/phone components and
    excluded handled contacts unless a genuine next step is due. Never mutate
    calls/tasks/human notes to make the list fit.
    """
    allowed = {i['lead_id']: i for i in current['items']}
    retained = [dict(i, **allowed[i['lead_id']]) for i in before['items'] if i['lead_id'] in allowed]
    if len(retained) > target:
        raise HTTPException(409, 'Existing plan exceeds target; manual review required')
    seen = {i['lead_id'] for i in retained}
    additions = [i for i in current['items'] if i['lead_id'] not in seen][:target-len(retained)]
    after = deepcopy(before)
    after.update(items=retained + additions, target_contacts=target,
        total=len(retained)+len(additions), version=before['version']+1,
        command_id=str(command_id), generated_at=datetime.now(UTC).isoformat(),
        reconciliation={'previous_command_id':before['command_id'], 'previous_version':before['version'],
            'removed_lead_ids':[i['lead_id'] for i in before['items'] if i['lead_id'] not in seen],
            'replacement_contacts_added':len(additions)},
        available_counterparties=current['available_counterparties'],
        excluded_candidates=current['excluded_candidates'])
    after['context_lead_ids'] = sorted(set(before.get('context_lead_ids', [i['lead_id'] for i in before['items']])) | {i['lead_id'] for i in after['items']})
    after['source_status'] = 'available' if after['total'] == target else 'partial'
    after['blockers'] = [current['blockers'][0]]
    if after['total'] != target:
        after['blockers'].append(f"Contactos elegíveis insuficientes: {after['total']} de {target}; owner Main/Sales")
    return after
