"""Read-only call facts. Historical absence is never a certified first call."""
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from src.crm.domain.call_contract import CallDetails
from src.crm.persistence.models import Activity, Lead
from src.crm.activity_provenance import operational_activity_filter


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
    seen = set()
    for activity, lead in rows:
        try:
            facts = CallDetails.model_validate(activity.call_details)
        except (ValueError, TypeError):
            facts = None
        today = start <= activity.occurred_at < end
        identity = lead.account_id or lead.id
        if today:
            counts['attempts'] += 1
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
    return {'schema_version':1, 'date':day.isoformat(), 'timezone':timezone_name,
            'source_status':'partial', 'counts':counts, 'coverage':coverage,
            'target_first_answered':10, 'deficit':None,
            'recorded_deficit':max(0,10-counts['first_answered_recorded']),
            'confirmed_deficit':max(0,10-counts['first_answered_confirmed']),
            'blockers':blockers, 'generated_at':datetime.now(UTC).isoformat()}
