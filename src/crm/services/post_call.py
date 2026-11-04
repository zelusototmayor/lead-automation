"""Transactional first post-call cycle; deterministic facts + durable Sales context.

No mail/LLM/provider client is imported here. Human facts are not inferred from
keywords. Full notes remain in immutable Activity; the visible digest is bounded.
"""
from datetime import UTC, datetime
from uuid import uuid5
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from src.crm.persistence.models import Lead, Contact, Account, AuditEvent
from src.crm.services.typed_decisions import evaluate_crm


def prepare_post_call(session, activity, digest, callback_task_id=None):
    from src.crm.services.note_source_service import note_context
    lead = session.scalar(select(Lead).where(Lead.workspace_id == activity.workspace_id,
                                           Lead.id == activity.lead_id))
    from src.crm.services.call_note_stage import project_note_stage
    commercial_state = project_note_stage(session, lead, activity, activity.actor_id) if lead else None
    source = note_context(session, activity.workspace_id, activity.lead_id,
                          {'activity_id': str(activity.id), 'source_digest': digest})
    account = session.get(Account, lead.account_id) if lead and lead.account_id else None
    contact = session.get(Contact, lead.contact_id) if lead and lead.contact_id else None
    details = activity.call_details or {}
    ref = {'activity_id': str(activity.id), 'source_digest': digest}
    decision_role = ('decision_maker' if details.get('decision_maker') is True
                     and details.get('interlocutor_role') == 'decision_maker'
                     else 'contact' if details.get('interlocutor_role') == 'reception'
                     else 'unknown')
    projection = {
        'version': 'post-call/v1', 'event_id': str(activity.id), 'correlation_id': digest,
        'source': source,
        'contact': {'name': contact.full_name if contact else lead.contact_name if lead else None,
                    'company': account.display_name if account else lead.company_name if lead else None},
        'qualification': {
            'interest': {'value': 'no' if activity.outcome_code == 'not_interested' else 'unknown',
                         'provenance': 'human_outcome' if activity.outcome_code == 'not_interested' else 'unknown',
                         'evidence_refs': [ref] if activity.outcome_code == 'not_interested' else []},
            'decision_role': {'value': decision_role, 'provenance': 'human_call_details',
                              'evidence_refs': [ref] if decision_role != 'unknown' else []},
            'relevance': {'value': 'unknown', 'authority': 'commercial_kpis_v1'},
            'result': {'value': activity.outcome_code or 'unknown', 'evidence_refs': [ref]},
        },
        'short_note': (activity.summary or '')[:280],
        'note_truncated': len(activity.summary or '') > 280,
        'priority': {'value': lead.priority if lead else 'unknown',
                     'reason': 'Prioridade CRM existente; sem promoção inferida'},
        'callback': {'task_id': str(callback_task_id) if callback_task_id else None,
                     'status': 'already_enqueued' if callback_task_id else 'review_context'},
        'commercial_state': commercial_state,
        'allowed_actions': ['crm_context', 'callback_review', 'sales_handoff'],
        'sales_handoff': {'owner': 'head-of-sales', 'status': 'queued',
                          'mail_composition_authorized': False, 'send_authorized': False},
        'jev': evaluate_crm(source, enabled=False),
        'generated_at': datetime.now(UTC).isoformat(),
    }
    session.execute(insert(AuditEvent).values(
        id=uuid5(activity.id, 'post-call/v1'), workspace_id=activity.workspace_id,
        command_id=uuid5(activity.id, 'post-call/v1'),
        actor_id=activity.actor_id or activity.workspace_id, action='post_call.context_prepared',
        entity_type='activity', entity_id=activity.id,
        details={'version': projection['version'], 'source_digest': digest,
                 'activity_id': str(activity.id), 'side_effects': 'CRM_context_only',
                 'handoff_kind': 'call_followup', 'jev_status': projection['jev']['status']},
    ).on_conflict_do_nothing(index_elements=['id']))
    return projection
