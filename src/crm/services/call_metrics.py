"""Read-only call facts. Historical absence is never a certified first call."""
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from hashlib import sha256
from sqlalchemy import select
from src.crm.domain.call_contract import CallDetails
from src.crm.persistence.models import Activity, Lead, SourceIdentity
from src.crm.activity_provenance import operational_activity_filter


# Human-reviewed legacy notes, bound to exact immutable event and content.
# Sales audit + live timeline reconciliation, 2026-09-08 (t_084a11a3).
# No text classifier: follow_up/not_interested alone never imply attendance.
# This registry certifies only human attendance, not usefulness/role/novelty.
REVIEWED_LEGACY_ANSWERS = {
    '130f5612-a89a-545e-91be-1ed35a464751': ('follow_up', '81c9f74752a0b05ff944365935c7669c987d16e95381354c573f3b54f9b814a3'),
    '634b0534-076e-5904-b742-01702cea62f0': ('not_interested', '35889047d1b035355854c7febb36c135d5389d160341794625dfeaa6661fd079'),
}


def legacy_contact_state(raw, occurred_at, zone):
    """Dated legacy evidence only; today's/current stage never rewrites history."""
    if not isinstance(raw, dict):
        return 'uncertain'
    uncertain = False
    for key in ('Initial Email Sent','Outreach FU1 Sent','Outreach FU2 Sent',
                'Proposal Sent','Proposal Email Sent','Last Contact'):
        value = raw.get(key)
        if value is None or value == '':
            continue
        try:
            value = value.strip()
            if not value:
                continue
            if 'T' in value:
                when = datetime.fromisoformat(value)
                if when.tzinfo is None:
                    uncertain = True
                elif when < occurred_at:
                    return 'prior'
            else:
                when = datetime.strptime(value, '%Y/%m/%d' if len(value.split('/', 1)[0]) == 4 else '%d/%m/%Y').date() if '/' in value else date.fromisoformat(value)
                call_day = occurred_at.astimezone(zone).date()
                if when < call_day:
                    return 'prior'
                uncertain |= when == call_day
        except (ValueError, TypeError, AttributeError):
            uncertain = True
    return 'uncertain' if uncertain else 'none'


def call_metrics(session, workspace_id, day, timezone_name='Europe/Lisbon'):
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    rows = session.execute(select(Activity, Lead).join(Lead,
        (Lead.id == Activity.lead_id) & (Lead.workspace_id == Activity.workspace_id))
        .where(Activity.workspace_id == workspace_id, Activity.activity_type == 'call',
               Activity.occurred_at < end, operational_activity_filter())
        .order_by(Activity.occurred_at, Activity.id)).all()
    counts = dict(attempts=0, answered=0, useful=0, decision_maker=0,
                  first_answered_recorded=0, first_answered_confirmed=0, first_answered=None, repeats=0)
    coverage = dict(answer_unknown=0, useful_unknown=0, decision_maker_unknown=0, legacy_history_unknown=0)
    invalid_details = 0
    seen = set()
    contact_counts = dict(first_contact=0, follow_up=0, unknown=0, new_companies=0)
    first_contact_at = {}
    # Include account-level messages/meetings with no lead_id as prior contact.
    for event, account_id in session.execute(select(Activity, Lead.account_id)
            .outerjoin(Lead, (Lead.id == Activity.lead_id) & (Lead.workspace_id == Activity.workspace_id))
            .where(Activity.workspace_id == workspace_id,
                   Activity.activity_type.in_(('call','email_sent','email_received','meeting','proposal')),
                   Activity.occurred_at < end, operational_activity_filter())
            .order_by(Activity.occurred_at, Activity.id)):
        identity = account_id or event.account_id or event.lead_id
        if identity is not None:
            first_contact_at.setdefault(identity, event.occurred_at)
    new_companies = set()
    legacy_by_identity = {}
    for source_lead, metadata in session.execute(select(Lead, SourceIdentity.metadata_json)
            .join(SourceIdentity, (SourceIdentity.id == Lead.source_identity_id) &
                  (SourceIdentity.workspace_id == Lead.workspace_id))
            .where(Lead.workspace_id == workspace_id)):
        legacy_by_identity.setdefault(source_lead.account_id or source_lead.id, []).append(
            (metadata or {}).get('legacy_row', {}))
    for activity, lead in rows:
        identity = lead.account_id or lead.id
        prior_contact = identity in first_contact_at and first_contact_at[identity] < activity.occurred_at

        try:
            facts = CallDetails.model_validate(activity.call_details)
            # Retained/imported rows may predate writer validation. Conflicting
            # structured evidence stays unknown, never repaired by fallback.
            facts.validate_outcome(activity.outcome_code)
            if activity.outcome_code == 'connected' and facts.answer_kind not in {'unknown', 'human_counterparty'}:
                raise ValueError('Connected conflicts with explicit non-human evidence')
        except (ValueError, TypeError):
            facts = None
        # Legacy outcomes certify attendance only, never usefulness or novelty.
        reviewed = REVIEWED_LEGACY_ANSWERS.get(str(activity.id))
        reviewed_answer = reviewed is not None and reviewed == (
            activity.outcome_code, sha256((activity.summary or '').encode()).hexdigest())
        legacy_kind = {'connected': 'human_counterparty', 'no_answer': 'no_answer',
                       'voicemail': 'voicemail', 'wrong_number': 'wrong_number'}.get(activity.outcome_code)
        if legacy_kind is None and reviewed_answer:
            legacy_kind = 'human_counterparty'
        if activity.call_details is None and legacy_kind is not None:
            facts = CallDetails(schema_version=1, attempted=True,
                answer_kind=legacy_kind,
                useful=None, decision_maker=None, interlocutor_role='unknown', repeat_reason=None)
        if facts is not None and facts.answer_kind == 'unknown' and legacy_kind is not None:
            facts = facts.model_copy(update={'answer_kind': legacy_kind})
        today = start <= activity.occurred_at < end
        identity = lead.account_id or lead.id
        if today:
            counts['attempts'] += 1
            invalid_details += activity.call_details is not None and facts is None
            legacy_states = {legacy_contact_state(raw, activity.occurred_at, zone)
                             for raw in legacy_by_identity.get(identity, [])}
            contact_kind = 'follow_up' if prior_contact or 'prior' in legacy_states else facts.contact_kind if facts else 'unknown'
            if contact_kind == 'first_contact' and 'uncertain' in legacy_states:
                contact_kind = 'unknown'
            contact_counts[contact_kind] += 1
            if contact_kind == 'first_contact':
                new_companies.add(identity)
            if facts is None or facts.answer_kind == 'unknown':
                coverage['answer_unknown'] += 1
            if facts is None or facts.useful is None:
                coverage['useful_unknown'] += 1
            if facts is None or facts.decision_maker is None:
                coverage['decision_maker_unknown'] += 1
        if facts is None or facts.answer_kind != 'human_counterparty':
            continue
        if today:
            counts['answered'] += 1
            counts['useful'] += facts.useful is True
            counts['decision_maker'] += facts.decision_maker is True
            if identity not in seen and facts.first_conversation is not False and (lead.phone_history or {}).get('state') != 'prior_answer':
                counts['first_answered_recorded'] += 1
                if facts.first_conversation is True:
                    counts['first_answered_confirmed'] += 1
                elif facts.first_conversation is None:
                    coverage['legacy_history_unknown'] += 1
            else:
                counts['repeats'] += 1
        seen.add(identity)
    blockers = ['Primeiras atendidas = primeiro registo no CRM; histórico anterior não certificado.']
    if invalid_details:
        blockers.append(f'{invalid_details} registo(s) com dimensões inválidas ou em conflito; atendimento desconhecido até revisão.')
    contact_counts['new_companies'] = len(new_companies)
    blockers.append('Primeiro contacto exige confirmação de ausência de contacto anterior em qualquer canal; histórico incompleto permanece desconhecido. Empresas novas não são oportunidades qualificadas.')
    return {'schema_version':1, 'date':day.isoformat(), 'timezone':timezone_name,
            'source_status':'partial', 'counts':counts, 'coverage':coverage, 'contact_counts':contact_counts,
            'target_first_answered':10, 'deficit':None,
            'recorded_deficit':max(0,10-counts['first_answered_recorded']),
            'confirmed_deficit':None if coverage['answer_unknown'] or coverage['legacy_history_unknown'] else max(0,10-counts['first_answered_confirmed']),
            'blockers':blockers, 'generated_at':datetime.now(UTC).isoformat()}
