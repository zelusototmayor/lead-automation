"""Project the existing owner attendance rule into New -> Contacted only.

The human source and interpretation receipts stay unchanged. Callers own the
lead lock/transaction; this helper creates no tasks, email or send authority.
"""
from datetime import UTC, datetime
from uuid import uuid5

from sqlalchemy import and_, or_, select

from src.crm.domain.stage_policy import highest_stage_rank, stage_rank
from src.crm.persistence.models import Activity, AuditEvent
from src.crm.services.note_source_service import source_digest
from src.crm.services.personal_call_rules import eligible, project

POLICY = 'owner-call-contact-stage/2026-10-08-v1'


def project_contact_stage(session, lead, source, actor_id, *, classification=None):
    """Apply once under the caller's current lead/source fences, never regress."""
    from src.crm.services.call_note_stage import project_note_stage
    explicit = project_note_stage(session, lead, source, actor_id)
    if explicit['status'] != 'not_requested':
        return explicit
    audit_id = uuid5(source.id, POLICY)
    previous = session.get(AuditEvent, audit_id)
    if previous is not None:
        if (previous.workspace_id != lead.workspace_id or previous.entity_id != lead.id
                or previous.details.get('source_digest') != source_digest(source)):
            raise ValueError('contact_stage_projection_identity_changed')
        return {'status': 'already_projected', 'audit_id': str(audit_id)}
    if (source.workspace_id != lead.workspace_id or source.lead_id != lead.id
            or lead.stage != 'new' or lead.highest_stage_rank > stage_rank('new')
            or not eligible(source)):
        return {'status': 'not_eligible'}
    details = source.call_details or {}
    # Structured explicit exclusions outrank the owner's attendance default.
    if (source.outcome_code in {'no_answer', 'voicemail', 'wrong_number', 'skip', 'skipped'}
            or details.get('answer_kind') in {'no_answer', 'voicemail', 'wrong_number',
                                             'ivr', 'ivr_only', 'automated_system'}
            or details.get('attempted') is False):
        return {'status': 'explicit_noncontact'}
    effective = project(source, classification)
    units = (effective or {}).get('units') or []
    facts = units[0].get('classification', {}) if len(units) == 1 else {}
    if ((facts.get('call') or {}).get('value') is not True
            or (facts.get('answered') or {}).get('value') is not True):
        return {'status': 'not_answered_call'}
    newer = session.scalar(select(Activity.id).where(
        Activity.workspace_id == lead.workspace_id, Activity.lead_id == lead.id,
        Activity.id != source.id,
        or_(Activity.supersedes_activity_id == source.id,
            and_(Activity.activity_type.in_(('call', 'stage_change')),
                 Activity.occurred_at > source.occurred_at),
            and_(Activity.activity_type == 'stage_change',
                 Activity.created_at > source.created_at)),
    ).limit(1))
    if newer is not None:
        return {'status': 'newer_human_context'}
    lead.stage = 'contacted'
    lead.highest_stage_rank = highest_stage_rank(lead.highest_stage_rank, 'contacted')
    lead.updated_at = datetime.now(UTC)
    session.add(AuditEvent(
        id=audit_id, workspace_id=lead.workspace_id, command_id=audit_id,
        actor_id=actor_id, action='lead.contact_stage_projected',
        entity_type='lead', entity_id=lead.id,
        details={'from_stage': 'new', 'to_stage': 'contacted', 'policy': POLICY,
                 'basis': 'existing_owner_answered_call_rule',
                 'source_activity_id': str(source.id), 'source_digest': source_digest(source),
                 'attendance_reason': (effective.get('business_rule_projection') or {}).get('reason'),
                 'source_and_receipt_unchanged': True, 'send_authority': False},
    ))
    return {'status': 'projected', 'audit_id': str(audit_id)}
