"""Canonical reversible personal-note projection. No tasks, emails or stages.

Uses the existing source/lead locks, durable Jev receipts and immutable AuditEvent
store. The human Activity is never rewritten or duplicated into a synthetic call.
"""
from copy import deepcopy
from datetime import UTC
import os
from uuid import UUID, uuid5
from sqlalchemy import select
from src.crm.persistence.models import Activity, AgentWork, AuditEvent
from src.crm.services.agent_work_service import WorkConflict, locked_work
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
from src.crm.services.note_source_service import source_digest
from src.crm.services.jev_note_policy import digest
from src.crm.services import jose_note_policy as policy
from src.crm.services import jose_note_policy_v2 as unit_policy
from src.crm.services import jose_note_units

ACTION = 'agent.personal_note_interpreted'
WITHDRAW = 'agent.personal_note_withdrawn'
V19_VERSION = 'historical-v19-embedded-date/live-patch4'
V19_READ_VERSIONS = ('historical-v19-embedded-date/live-patch3', V19_VERSION)


def current_audit(source, records, *, superseded=False):
    records = sorted(records, key=lambda row:(row.created_at, row.id))
    withdrawn = {row.details['target_audit_id'] for row in records if row.action == WITHDRAW}
    current = [row for row in records if row.action == ACTION and str(row.id) not in withdrawn
        and row.details.get('source_digest') == source_digest(source)
        and row.details.get('policy_version') in (policy.VERSION, unit_policy.IMPORT_CONVENTION_VERSION, *V19_READ_VERSIONS)]
    # PostgreSQL now() is transaction-scoped: two append-only audits written in
    # one transaction can share created_at. A correction's explicit predecessor
    # link outranks UUID tie-breaking, while withdrawing it restores the prior.
    predecessors = {row.details.get('before_audit_id') for row in current}
    heads = [row for row in current if str(row.id) not in predecessors]
    return heads[-1] if heads and not superseded else None


def source_snapshot_units(source):
    return unit_policy.source_units({'workspace_id':str(source.workspace_id),'lead_id':str(source.lead_id),
        'source':{'activity_id':str(source.id),'source_digest':source_digest(source),'status':'current',
            'activity_type':source.activity_type,'summary':source.summary,'outcome_code':source.outcome_code,
            'call_details':source.call_details,'occurred_at':source.occurred_at.isoformat(),
            'provenance':{'actor_type':source.actor_type,'source_system':source.source_system,'title':source.title}}})


def _receipt_classification(session, source, audit):
    """Bounded audit points to the existing hashed receipt, not duplicated PII."""
    if audit is None:
        if source.activity_type != 'call': return None
        request=unit_policy.build_request(source_snapshot_units(source)[0])
        return unit_policy.project(request,{})
    if audit.details['policy_version']==policy.VERSION:
        return deepcopy(audit.details['classification'])
    if audit.details['policy_version'] in V19_READ_VERSIONS:
        from src.crm.services.jose_v19_import import KIND, receipt_context_current, source_hash, source_row
        row = session.get(AgentWork,UUID(audit.details['receipt_id']))
        if (row is None or row.workspace_id != source.workspace_id or row.lead_id != source.lead_id
                or row.kind != KIND or row.result_hash != digest(row.result)
                or row.result.get('status') != 'succeeded' or row.result.get('simulated') is not False
                or row.result.get('version') != audit.details['policy_version']
                or row.result.get('source_digest') != source_digest(source)
                or row.result.get('event_id') != str(source.id)
                or row.payload.get('source_digest') != source_digest(source)
                or digest(row.result.get('decisions')) != audit.details.get('classification_sha256')):
            raise ValueError('v19_classification_binding_changed')
        # Source/prior evidence and company/person changes invalidate call facts.
        # Email/phone maintenance and canonical contact normalization do not.
        # Receipt integrity and the immutable original audit remain mandatory.
        try:
            context = source_row(session,source.workspace_id,source.lead_id)
        except ValueError as exc:
            if str(exc) not in ('v19_context_missing', 'v19_lead_missing'):
                raise
            return None
        if (row.result.get('source_sha256') != source_hash(context,str(source.id))
                or not receipt_context_current(session, source, row, context)):
            return None
        return deepcopy(row.result['decisions'])
    row=session.get(AgentWork,UUID(audit.details['receipt_id']))
    if (row is None or row.workspace_id!=source.workspace_id or row.lead_id!=source.lead_id
            or row.kind!='jev_note_decision' or row.result_hash!=digest(row.result)
            or row.payload.get('event_id')!=str(source.id)
            or row.payload.get('source_digest')!=source_digest(source)
            or row.result.get('status')!='succeeded' or row.result.get('simulated') is not False
            or row.result.get('version')!=unit_policy.IMPORT_CONVENTION_VERSION):
        raise ValueError('personal_unit_receipt_changed')
    value=row.result['decisions']
    if digest(value)!=audit.details.get('classification_sha256'):
        raise ValueError('personal_unit_projection_changed')
    if [u['unit'] for u in value['units']] != [s['source']['unit'] for s in source_snapshot_units(source)]:
        raise ValueError('personal_unit_binding_changed')
    return deepcopy(value)


def audit_classification(session, source, audit):
    """Keep verified receipt evidence separate from prospective owner rules."""
    from src.crm.services.personal_call_rules import project
    return project(source, _receipt_classification(session, source, audit))


def read_classification(session, workspace_id, lead_id, activity_id):
    activity_id = UUID(str(activity_id))
    source = session.scalar(select(Activity).where(Activity.workspace_id == workspace_id,
        Activity.lead_id == lead_id, Activity.id == activity_id))
    if source is None:
        raise LookupError('source_unavailable')
    signature = source_digest(source)
    superseded = session.scalar(select(Activity.id).where(Activity.workspace_id == workspace_id,
        Activity.supersedes_activity_id == source.id).limit(1)) is not None
    records = list(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id,
        AuditEvent.entity_type == 'activity', AuditEvent.entity_id == source.id,
        AuditEvent.action.in_((ACTION, WITHDRAW))).order_by(AuditEvent.created_at, AuditEvent.id)))
    latest = current_audit(source, records, superseded=superseded)
    # Import recording timestamps do not certify an interaction date.
    occurred = None if source.actor_type in ('migration','import','system') else source.occurred_at.astimezone(UTC).isoformat()
    # The business rule may already produce attendance while the model receipt
    # is absent/stale. Keep that distinction so the worker still interprets DM
    # context and actions rather than treating the default as a model result.
    from src.crm.services.personal_call_rules import project
    receipt_classification = _receipt_classification(session,source,latest) if not superseded else None
    classification = project(source, receipt_classification) if not superseded else None
    receipt_status = ('absent' if latest is None else
        'stale' if receipt_classification is None else 'current')
    return {'activity_id': str(source.id), 'source_digest': signature,
        'source_status': 'superseded' if superseded else 'current',
        'classification': classification,
        'classification_status': ('stale_context' if latest and
            latest.details['policy_version'] in V19_READ_VERSIONS and receipt_classification is None else
            'current' if latest and receipt_classification is not None else 'unclassified'),
        'model_receipt_status': receipt_status,
        'audit_id': str(latest.id) if latest else None,
        'receipt_id': latest.details['receipt_id'] if latest else None,
        'occurred_at': occurred, 'recording_date_only': occurred is None,
        'policy_version': latest.details['policy_version'] if latest else unit_policy.IMPORT_CONVENTION_VERSION,
        'effective_policy_version': ((classification or {}).get('business_rule_projection') or {}).get('version')
            or (latest.details['policy_version'] if latest else unit_policy.IMPORT_CONVENTION_VERSION),
        'original_activity_unchanged': True}


def apply_classification(session, principal, work_id, body):
    # The old Jev endpoint may still be reachable from an installed Sales
    # consumer. During v19 activation it cannot publish a newer old-policy audit.
    if os.environ.get('CRM_V19_PERSONAL_NOTES_ENABLED') == '1':
        raise WorkConflict('Legacy personal-note classifier disabled by v19')
    from src.crm.services.jev_note_worker import _decision_row, _current_request
    from src.crm.services.jev_note_runtime import target_identity
    work = locked_work(session, principal.workspace_id, work_id)
    lead, _ = _locked_company_lead(session, principal.workspace_id, work.lead_id)
    if work.kind != 'call_followup' or lead.version != body.expected_lead_version:
        raise WorkConflict('Personal-note context changed')
    source_id = UUID(work.payload['activity_id'])
    sources = _locked_rows(session, select(Activity).where(Activity.workspace_id == principal.workspace_id,
        Activity.lead_id == lead.id))
    source = next((row for row in sources if row.id == source_id), None)
    if source is None or source_digest(source) != body.source_digest or work.payload['source_digest'] != body.source_digest:
        raise WorkConflict('Personal-note source changed')
    row = _decision_row(session, principal.workspace_id, str(body.jev_receipt_id), lock=True)
    result = row.result or {}
    active_policy=jose_note_units if result.get('version')==jose_note_units.VERSION else policy
    if (row.lead_id != lead.id or result.get('work_id') != str(work_id)
            or result.get('version') != active_policy.VERSION or row.payload.get('flags') != active_policy.FLAGS
            or row.payload.get('event_id') != str(source.id) or row.payload.get('source_digest') != body.source_digest
            or result.get('simulated') is not False or result.get('target') != target_identity(session)
            or row.result_hash != digest(result)):
        raise WorkConflict('Personal-note receipt binding invalid')
    _current_request(session, row)
    audit_id = uuid5(row.id, ACTION)
    previous = session.get(AuditEvent, audit_id)
    if previous is not None:
        if previous.workspace_id != principal.workspace_id or previous.details['receipt_id'] != str(row.id):
            raise WorkConflict('Personal-note replay changed')
        return {'audit_id':str(audit_id), 'replayed':True, 'source_work_unchanged':True,
            'classification':audit_classification(session,source,previous)}
    classification = deepcopy(result['decisions']) if active_policy is jose_note_units else {
        field:{key:deepcopy(item[key]) for key in ('value','probability','confidence','confidence_role','reason')}
        for field,item in result['decisions'].items()}
    details = {'receipt_id':str(row.id),'work_id':str(work.id),'source_digest':body.source_digest,
        'policy_version':active_policy.VERSION,
        'original_activity_unchanged':True,'source_work_unchanged':True}
    if active_policy is jose_note_units:
        if [u['unit'] for u in classification['units']] != [s['source']['unit'] for s in source_snapshot_units(source)]:
            raise WorkConflict('Personal-note units changed')
        details['classification_sha256']=digest(classification)
    else:details['classification']=classification
    session.add(AuditEvent(id=audit_id,workspace_id=principal.workspace_id,command_id=audit_id,
        actor_id=principal.actor_id,entity_type='activity',entity_id=source.id,action=ACTION,details=details))
    session.flush()
    return {'audit_id':str(audit_id), 'replayed':False, 'source_work_unchanged':True,
        'classification':classification}


def withdraw_classification(session, principal, work_id, body):
    """Append a tombstone for one interpretation; never edit source or work."""
    work = locked_work(session, principal.workspace_id, work_id)
    lead, _ = _locked_company_lead(session, principal.workspace_id, work.lead_id)
    target = session.get(AuditEvent, UUID(str(body.audit_id)))
    if (work.kind != 'call_followup' or lead.version != body.expected_lead_version
            or target is None or target.workspace_id != principal.workspace_id
            or target.action != ACTION or target.entity_type != 'activity'
            or target.details.get('work_id') != str(work_id)
            or target.details.get('source_digest') != body.source_digest
            or str(target.entity_id) != work.payload.get('activity_id')):
        raise WorkConflict('Personal-note withdrawal binding changed')
    # Rollback remains possible after a later source edit. Only the identified
    # interpretation is withdrawn, not the new source or another projection.
    audit_id = uuid5(target.id, WITHDRAW)
    previous = session.get(AuditEvent, audit_id)
    if previous is not None:
        if previous.workspace_id != principal.workspace_id or previous.action != WITHDRAW:
            raise WorkConflict('Personal-note withdrawal collision')
        return {'audit_id':str(audit_id), 'replayed':True, 'source_work_unchanged':True}
    session.add(AuditEvent(id=audit_id, workspace_id=principal.workspace_id, command_id=body.command_id,
        actor_id=principal.actor_id, entity_type='activity', entity_id=target.entity_id,
        action=WITHDRAW, details={'target_audit_id':str(target.id), 'work_id':str(work_id),
            'source_digest':body.source_digest, 'original_activity_unchanged':True,
            'source_work_unchanged':True}))
    session.flush()
    return {'audit_id':str(audit_id), 'replayed':False, 'source_work_unchanged':True}
