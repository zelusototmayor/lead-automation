"""Source-bound Jev provenance and canonical derived-fact projection.

Jev interest remains advisory: this bridge never translates it into usefulness,
authority, stage, financials or tasks. Sales still submits the complete cited
plan to the existing writer, under its existing lease and action safeguards.
"""
from copy import deepcopy

from sqlalchemy import or_, select

from src.crm.domain.call_contract import CallDetails
from src.crm.persistence.models import Activity, AuditEvent
from src.crm.services.agent_work_service import WorkConflict
from src.crm.services.note_source_service import source_digest
from src.crm.services.note_fact_projection import merge_facts, reviewed_candidates, WITHDRAWAL_ACTION


def bind_receipt(session, workspace_id, work, lead, receipt_id, *, factual=False):
    """Lock both receipt and context; a caller's opaque ID is not authority."""
    from src.crm.services.jev_note_runtime import target_identity
    from src.crm.services.jev_note_worker import _current_request, _decision_row
    from src.crm.services.note_plan_service import _locked_rows

    row = _decision_row(session, workspace_id, str(receipt_id), lock=True)
    result = row.result or {}
    if (row.lead_id != lead.id or result.get('simulated') is not False
            or result.get('work_id') != str(work.id)
            or result.get('target') != target_identity(session)
            or row.payload.get('event_id') != work.payload.get('activity_id')
            or row.payload.get('source_digest') != work.payload.get('source_digest')
            or (not factual and result.get('application', {}).get('status') != 'applied')):
        raise WorkConflict('Jev receipt binding invalid')
    if factual:
        from src.crm.services.jev_call_facts import supported_policy_versions
        versions = supported_policy_versions()
        if result.get('version') not in versions or row.payload.get('flags') != versions[result['version']]:
            raise WorkConflict('Factual receipt required')
    scope = or_(Activity.lead_id == lead.id, Activity.account_id == lead.account_id) if lead.account_id else Activity.lead_id == lead.id
    _locked_rows(session, select(Activity).where(Activity.workspace_id == workspace_id, scope))
    _current_request(session, row)
    return {'receipt_id': str(row.id), 'source_digest': row.payload['source_digest'],
        'snapshot_digest': result['snapshot_digest'], 'model': result['model'],
        'policy_version': result['version'], 'points': sorted(result['decisions']),
        'fact_authority': False}


def read_facts(session, workspace_id, lead_id, activity_id):
    source = session.scalar(select(Activity).where(Activity.workspace_id == workspace_id,
        Activity.lead_id == lead_id, Activity.id == activity_id))
    if source is None:
        raise LookupError('source_unavailable')
    digest = source_digest(source)
    superseded = session.scalar(select(Activity.id).where(Activity.workspace_id == workspace_id,
        Activity.supersedes_activity_id == activity_id).limit(1)) is not None
    human = deepcopy(source.call_details)
    candidates, provenance, interests, reviews = [], [], [], []
    if not superseded:
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_id == activity_id, AuditEvent.entity_type == 'activity',
                AuditEvent.action == 'agent.note_interpreted').order_by(AuditEvent.created_at, AuditEvent.id)))
        audits = [row for row in audits if (row.details or {}).get('source_digest') == digest]
        withdrawals = list(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_id == activity_id, AuditEvent.entity_type == 'activity',
                AuditEvent.action == WITHDRAWAL_ACTION).order_by(AuditEvent.created_at, AuditEvent.id)))
        candidates, removed, reviews = reviewed_candidates(audits, withdrawals)
        from src.crm.services.jev_note_policy import digest as payload_digest
        for row in audits:
            data = row.details or {}
            if 'expressed_commercial_interest' in data:
                interests.append(data['expressed_commercial_interest'])
            provenance.append({'audit_id': str(row.id), 'work_id': data.get('work_id'),
                'source_digest': digest, 'plan_hash': data.get('plan_hash'), 'jev': data.get('jev'),
                'facts': data.get('facts', {}), 'audit_payload_digest': payload_digest(data),
                'withdrawn_fields': sorted(removed.get(str(row.id), set()))})
    derived, conflict = merge_facts(candidates)
    effective = deepcopy(human)
    if source.activity_type == 'call' and derived:
        effective = dict(schema_version=1, attempted=True, answer_kind='unknown', useful=None,
            decision_maker=None, interlocutor_role='unknown', repeat_reason=None)
        effective.update(human or {})
        for field, value in derived.items():
            if effective.get(field) in (None, 'unknown'):
                effective[field] = value
        try:
            parsed = CallDetails.model_validate(effective)
            parsed.validate_outcome(source.outcome_code)
        except (ValueError, TypeError):
            effective, derived, conflict = deepcopy(human), {}, True
    from src.crm.services.expressed_interest import read_interest
    interest = read_interest(session, source, interests, superseded=superseded)
    return {'activity_id': str(activity_id), 'source_digest': digest,
        'source_status': 'superseded' if superseded else 'current',
        'human_call_details': human, 'derived_facts': derived,
        'effective_call_details': effective, 'conflict': conflict, 'provenance': provenance,
        'adjudications': reviews,
        'projection_status': 'reviewed_adjudication' if reviews else 'interpreted',
        'expressed_commercial_interest': interest,
        'original_activity_unchanged': True}
