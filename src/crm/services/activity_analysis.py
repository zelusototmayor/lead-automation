"""Aggregate registered commercial activity, never inferred mailbox history."""
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from src.crm.activity_provenance import operational_activity_filter
from src.crm.domain.call_contract import CallDetails
from src.crm.persistence.models import Activity, Lead, SourceIdentity
from src.crm.services.call_metrics import legacy_contact_state

SERIES = {
    'email_initial': 'Emails iniciais enviados',
    'email_follow_up': 'Emails de follow-up enviados',
    'call_initial': 'Chamadas para novas leads',
    'call_follow_up': 'Chamadas de follow-up',
}


def activity_analysis(session, workspace_id, day, days=30):
    zone = ZoneInfo('Europe/Lisbon')
    dates = [(day - timedelta(days=i)).isoformat() for i in reversed(range(days))]
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    rows = session.execute(select(Activity, Lead.account_id, SourceIdentity)
        .outerjoin(Lead, (Lead.id == Activity.lead_id) & (Lead.workspace_id == Activity.workspace_id))
        .outerjoin(SourceIdentity, (SourceIdentity.id == Activity.source_identity_id) &
                   (SourceIdentity.workspace_id == Activity.workspace_id))
        .where(Activity.workspace_id == workspace_id, Activity.occurred_at < end,
               Activity.activity_type.in_(('call', 'email_sent', 'email_received', 'meeting', 'proposal')),
               operational_activity_filter())
        .order_by(Activity.occurred_at, Activity.id)).all()
    legacy = {}
    for lead, metadata in session.execute(select(Lead, SourceIdentity.metadata_json)
            .join(SourceIdentity, (SourceIdentity.id == Lead.source_identity_id) &
                  (SourceIdentity.workspace_id == Lead.workspace_id))
            .where(Lead.workspace_id == workspace_id)):
        legacy.setdefault(lead.account_id or lead.id, []).append((metadata or {}).get('legacy_row', {}))
    counts = {key: {d: 0 for d in dates} for key in SERIES}
    unknown_days = {'email': set(), 'call': set()}
    coverage = {'email_unknown': 0, 'email_without_message_identity': 0, 'call_unknown': 0}
    first_contact, seen_messages = {}, set()
    for event, account, source in rows:
        identity = account or event.account_id or event.lead_id
        if identity is None:
            continue
        channel = 'call' if event.activity_type == 'call' else 'email'
        if source and source.source_system == 'gmail' and source.entity_kind == 'message':
            message_key = (source.source_scope, source.external_id)
            if message_key in seen_messages:
                continue
            seen_messages.add(message_key)
        prior = identity in first_contact and first_contact[identity] < event.occurred_at
        bucket = event.occurred_at.astimezone(zone).date().isoformat()
        contact_event = event.activity_type in ('call', 'email_received', 'meeting', 'proposal') or (
            event.activity_type == 'email_sent' and event.direction == 'outbound'
        )
        if contact_event:
            first_contact.setdefault(identity, event.occurred_at)
        if bucket not in counts['call_initial'] or event.activity_type not in ('call', 'email_sent'):
            continue
        states = {legacy_contact_state(raw, event.occurred_at, zone) for raw in legacy.get(identity, [])}
        kind = 'follow_up' if prior or 'prior' in states else 'unknown'
        if channel == 'call' and kind == 'unknown':
            try:
                facts = CallDetails.model_validate(event.call_details)
                facts.validate_outcome(event.outcome_code)
                if event.outcome_code == 'connected' and facts.answer_kind not in ('unknown', 'human_counterparty'):
                    raise ValueError('Conflicting attendance')
                kind = facts.contact_kind
                if kind == 'first_contact' and 'uncertain' in states:
                    kind = 'unknown'
            except (ValueError, TypeError):
                pass
        if channel == 'email' and event.direction != 'outbound':
            continue
        if channel == 'email' and kind == 'unknown' and 'uncertain' not in states:
            kind = 'first_contact'
        if kind == 'unknown':
            coverage[channel + '_unknown'] += 1
            unknown_days[channel].add(bucket)
        else:
            counts[channel + ('_initial' if kind == 'first_contact' else '_follow_up')][bucket] += 1
    return {
        'schema_version': 1, 'timezone': zone.key, 'days': days,
        'start_date': dates[0], 'end_date': dates[-1],
        'source_status': 'partial',
        'series': {key: {'label': label, 'total': sum(counts[key].values()),
                         'points': [{'date': d, 'value': counts[key][d] if counts[key][d] else
                                     (None if d in unknown_days[key.split('_')[0]] else 0)} for d in dates]}
                   for key, label in SERIES.items()},
        'coverage': coverage,
        'notes': ['Registos CRM de emails assinalados como enviados; cobertura histórica parcial.',
                  'Emails sem tipo explícito são classificados pelo histórico comercial registado.'],
        'generated_at': datetime.now(UTC).isoformat(),
    }
