"""Finite changed-cause admission, not a generic retry or a business writer.

Admission leaves FAILED/attempt3/history intact. Only an explicit normal claim by
our existing event worker can consume it, once, followed by the leased note writer.
"""
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5
import hashlib
import json
from sqlalchemy import select, and_
from src.crm.persistence.models import (Activity, AgentWork, AuditEvent, Lead, Task,
                                        Account, Contact, SourceIdentity)
from src.crm.services import moveauto_callback_reconciliation as bound
from src.crm.services.agent_work_service import WorkConflict, lead_is_suppressed
from src.crm.services.note_source_service import note_context

MARKER = 'moveauto_operational_admission_v1'
POLICY = 'moveauto24-changed-cause-admission/v1'
SOURCE_ARTIFACT = '59e0b378b5557d75f5e9255241f3228a06f359f1b7a6671257c1864f70134b40'
WORKER = 'hermes-head-of-sales-mac-mini-event'
# This file's content, unlike a deployment tag, proves the reviewed source semantics.
SOURCE_MODULE_SHA256 = '3699351518127bcb77c9f3dac21e2b08186cfc9455426e00c8ba38b522030ad2'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, default=str, allow_nan=False).encode()).hexdigest()


def record(row):
    return {c.key: getattr(row, c.key) for c in row.__mapper__.column_attrs}


def work_digest(row):
    data = record(row)
    data['payload'] = {k: v for k, v in (row.payload or {}).items() if k != MARKER}
    return digest(data)


def principal_for(workspace):
    return SimpleNamespace(workspace_id=workspace, actor_id=uuid5(workspace, 'head-of-sales'))


def plan_for(row, lead, source):
    from dashboard.app.routers.agent_work import NotePlanBody
    return NotePlanBody(lease_token=UUID(int=0), expected_lead_version=lead.version,
        source_digest=source['source_digest'], facts=[], contacts=[],
        actions=[dict(key=bound.KEY, task_type='call', title=bound.TITLE,
                      due_at=bound.DUE, quote=source['summary'])], summary='Finite callback reconciliation')


def _identity(principal, row):
    if (principal.workspace_id != bound.WORKSPACE
            or principal.actor_id != uuid5(bound.WORKSPACE, 'head-of-sales')
            or row.workspace_id != bound.WORKSPACE or row.id not in (bound.PRIMARY, bound.SECONDARY)
            or row.kind != 'call_followup' or row.lead_id != bound.LEAD or row.task_id is not None
            or (row.payload or {}).get('activity_id') != str(bound.NOTE if row.id == bound.PRIMARY else bound.CALL)
            or (row.payload or {}).get('source_digest') != bound.SOURCES[bound.NOTE if row.id == bound.PRIMARY else bound.CALL][1]
            or hashlib.sha256(Path(bound.__file__).read_bytes()).hexdigest() != SOURCE_MODULE_SHA256):
        raise WorkConflict('Operational admission exact identity changed')


def context_digest(session, principal, row):
    """Company/source/lead/contact/task/effect snapshot, rechecked at every boundary."""
    from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
    _identity(principal, row)
    lead, lids = _locked_company_lead(session, principal.workspace_id, row.lead_id)
    if lead_is_suppressed(session, lead):
        raise WorkConflict('Operational admission lead suppressed')
    source = note_context(session, bound.WORKSPACE, bound.LEAD, row.payload)
    if source.get('status') != 'current':
        raise WorkConflict('Operational admission source not current')
    body = plan_for(row, lead, source)
    binding = bound.validate_binding(session, principal, row, lead, body, source, _locked_rows)
    if not binding:
        raise WorkConflict('Operational admission source binding missing')
    if session.scalar(select(AuditEvent.id).where(AuditEvent.workspace_id == bound.WORKSPACE,
            AuditEvent.action == 'agent.note_interpreted',
            AuditEvent.details['work_id'].astext == str(row.id))):
        raise WorkConflict('Operational admission prior effect')
    scope = lambda model: (model.lead_id.in_(lids) | (model.account_id == bound.ACCOUNT))
    tasks = _locked_rows(session, select(Task).where(Task.workspace_id == bound.WORKSPACE,
                                                     scope(Task)).order_by(Task.id))
    works = _locked_rows(session, select(AgentWork).where(AgentWork.workspace_id == bound.WORKSPACE,
        AgentWork.lead_id.in_(lids), AgentWork.id != row.id).order_by(AgentWork.id))
    if row.id == bound.PRIMARY:
        if tasks or any(w.kind == 'calendar_callback' for w in works):
            raise WorkConflict('Operational admission prior callback effect')
    else:
        _primary_receipt(session, binding, tasks, works)
    # Full mapped rows: version-only checks would miss human/suppression/effect drift.
    rows = [('lead', _locked_rows(session, select(Lead).where(Lead.workspace_id == bound.WORKSPACE,
                Lead.id.in_(lids)).order_by(Lead.id))),
        ('account', _locked_rows(session, select(Account).where(Account.workspace_id == bound.WORKSPACE,
                Account.id == bound.ACCOUNT))),
        ('contact', _locked_rows(session, select(Contact).where(Contact.workspace_id == bound.WORKSPACE,
                (Contact.account_id == bound.ACCOUNT) | Contact.id.in_(select(Lead.contact_id).where(Lead.id.in_(lids))))
                .order_by(Contact.id))),
        ('identity', _locked_rows(session, select(SourceIdentity).where(SourceIdentity.workspace_id == bound.WORKSPACE,
                SourceIdentity.id.in_(select(Lead.source_identity_id).where(Lead.id.in_(lids)))).order_by(SourceIdentity.id))),
        ('activity', _locked_rows(session, select(Activity).where(Activity.workspace_id == bound.WORKSPACE,
                scope(Activity)).order_by(Activity.id))), ('task', tasks), ('work', works)]
    if lead_is_suppressed(session, lead):
        raise WorkConflict('Operational admission locked suppression changed')
    return digest({kind: [record(r) for r in items] for kind, items in rows})


def _primary_receipt(session, binding, tasks, works):
    primary = next((w for w in works if w.id == bound.PRIMARY), None)
    expected = uuid5(bound.PRIMARY, 'note-action:' + bound.KEY)
    result = primary.result if primary else None
    refs = result.get('tasks', []) if result else []
    if (not primary or primary.status != 'completed' or not primary.result_hash
            or not result or result.get('callback_reconciliation') != binding
            or result.get('evidence') != binding['evidence'] or len(tasks) != 1 or len(refs) != 1):
        raise WorkConflict('Primary committed receipt required before secondary admission')
    task = tasks[0]
    if (task.id != expected or str(task.id) != refs[0].get('task_id') or task.version != 1
            or task.status != 'open' or task.lead_id != bound.LEAD or task.account_id != bound.ACCOUNT
            or task.task_type != 'call' or task.due_at != bound.DUE or task.title != bound.TITLE
            or task.source_rule != 'head_of_sales_note' or task.call_intent is not None
            or task.owner_user_id != uuid5(bound.WORKSPACE, 'head-of-sales')):
        raise WorkConflict('Primary canonical task changed')
    callbacks = [w for w in works if w.kind == 'calendar_callback']
    if (len(callbacks) != 1 or str(callbacks[0].id) != refs[0].get('calendar_work_id')
            or callbacks[0].task_id != expected or callbacks[0].lead_id != bound.LEAD
            or callbacks[0].source_key != f'callback:{expected}:v1'
            or callbacks[0].payload.get('task_version') != 1):
        raise WorkConflict('Primary matching Calendar work receipt missing')


def _failed(row):
    if (row.status != 'failed' or row.attempts != 3 or row.result is not None
            or row.result_hash is not None or row.lease_token is not None
            or (row.lease_until is not None and row.lease_until > datetime.now(UTC))):
        raise WorkConflict('Operational admission requires unchanged failed exhausted work')


def preview(session, principal, row):
    _identity(principal, row)
    _failed(row)
    if MARKER in row.payload:
        return dict(row.payload[MARKER], replayed=True)
    context = context_digest(session, principal, row)
    return dict(policy=POLICY, work_id=str(row.id), actor_id=str(principal.actor_id),
        worker_id=WORKER, source_artifact=SOURCE_ARTIFACT, action_key=bound.KEY,
        prior_error=row.error, prior_worker_id=row.worker_id,
        prior_last_lease_token=str(row.last_lease_token) if row.last_lease_token else None,
        work_digest=work_digest(row), context_digest=context,
        audit_id=str(uuid5(row.id, POLICY)))


def admit(session, principal, row, expected):
    _identity(principal, row)
    audit_id = uuid5(row.id, POLICY)
    old = session.get(AuditEvent, audit_id)
    if old:
        if (old.workspace_id != principal.workspace_id or old.actor_id != principal.actor_id
                or old.action != POLICY or old.entity_id != row.id or old.details != expected):
            raise WorkConflict('Operational admission replay changed')
        return dict(old.details, replayed=True)
    candidate = preview(session, principal, row)
    if candidate != expected or MARKER in row.payload:
        raise WorkConflict('Operational admission CAS changed')
    session.add(AuditEvent(id=audit_id, workspace_id=principal.workspace_id,
        command_id=audit_id, actor_id=principal.actor_id, action=POLICY,
        entity_type='agent_work', entity_id=row.id, details=candidate))
    row.payload = dict(row.payload, **{MARKER: candidate})
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(row, 'updated_at')  # Explicitly retain, not rewrite, the original CAS timestamp.
    # Preserve status, attempts, error, result, timestamps, previous lease/audits/credits.
    session.flush()
    return dict(candidate, replayed=False)


def claim_filter(*, explicit):
    if not explicit:
        from sqlalchemy import false
        return false()
    return and_(AgentWork.id.in_((bound.PRIMARY, bound.SECONDARY)),
        AgentWork.workspace_id == bound.WORKSPACE, AgentWork.status == 'failed',
        AgentWork.attempts == 3, AgentWork.payload[MARKER]['policy'].astext == POLICY,
        ~AgentWork.payload[MARKER].has_key('consumed_lease'))


def consume(session, row, *, worker_id, actor_id):
    principal = principal_for(row.workspace_id)
    marker = (row.payload or {}).get(MARKER)
    if (not marker or worker_id != WORKER or actor_id != principal.actor_id
            or 'consumed_lease' in marker):
        raise WorkConflict('Operational admission ownership or one-use conflict')
    _identity(principal, row)
    _failed(row)
    old = session.get(AuditEvent, uuid5(row.id, POLICY))
    if (not old or old.details != marker or old.actor_id != actor_id
            or old.workspace_id != row.workspace_id or old.entity_id != row.id
            or marker.get('work_digest') != work_digest(row)
            or marker.get('context_digest') != context_digest(session, principal, row)):
        raise WorkConflict('Operational admission evidence drift')
    return marker


def record_consumption(session, row, marker, actor_id):
    details = dict(marker, consumed_lease=str(row.lease_token), consumed_at=datetime.now(UTC).isoformat())
    row.payload = dict(row.payload, **{MARKER: details})
    aid = uuid5(row.id, POLICY + ':consumed')
    session.add(AuditEvent(id=aid, workspace_id=row.workspace_id, command_id=aid,
        actor_id=actor_id, action=POLICY + ':consumed', entity_type='agent_work',
        entity_id=row.id, details=details))


def validate_plan(session, principal, row, body):
    marker = (row.payload or {}).get(MARKER)
    if not marker:
        return
    _identity(principal, row)
    audit = session.get(AuditEvent, uuid5(row.id, POLICY + ':consumed'))
    if (not audit or audit.details != marker or audit.actor_id != principal.actor_id
            or marker.get('consumed_lease') != str(body.lease_token)
            or row.worker_id != WORKER or row.attempts != 3
            or marker.get('context_digest') != context_digest(session, principal, row)):
        raise WorkConflict('Operational admitted context or lease changed')
    source = note_context(session, bound.WORKSPACE, bound.LEAD, row.payload)
    expected = plan_for(row, session.get(Lead, bound.LEAD), source)
    fields = {'actions', 'facts', 'contacts', 'disposition', 'jev_receipt_id', 'source_digest', 'expected_lead_version'}
    if body.model_dump(include=fields) != expected.model_dump(include=fields):
        raise WorkConflict('Operational admission requires the reviewed distinct intent')
