"""Read-only decision-maker conversations from the canonical fact projection.

José's conversation signal stays independent of expressed buying interest,
qualification and substantive relevance. Never classify note text here.
"""
from src.crm.domain.call_contract import CallDetails
from src.crm.services.jev_note_writer_bridge import read_facts


def conversation(details, outcome):
    try:
        facts = CallDetails.model_validate(details)
        facts.validate_outcome(outcome)
        if (outcome == 'connected' and facts.answer_kind not in ('unknown', 'human_counterparty')
                or facts.interlocutor_role == 'reception' and facts.decision_maker is True):
            return 'unknown'
    except (ValueError, TypeError):
        return 'unknown'
    if facts.answer_kind == 'human_counterparty' and facts.decision_maker is True:
        return 'yes'
    if facts.answer_kind not in ('unknown', 'human_counterparty') or facts.decision_maker is False:
        return 'no'
    return 'unknown'


def project(session, workspace_id, index, *, start, end, hot_start, hot_end):
    counts = dict(yes=0, no=0, unknown=0)
    interests = dict(yes=0, no=0, unknown=0)
    interest_reasons = {}
    hot = {}
    for row in index.activities.values():
        if (row.activity_type != 'call' or row.id in index.exclusions
                or not (start <= row.occurred_at < end or hot_start <= row.occurred_at < hot_end)):
            continue
        value = 'unknown'
        interest = {'value': 'unknown', 'reason': 'source_conflict'}
        if row.lead_id is not None and row.id not in index.conflicts:
            projection = read_facts(session, workspace_id, row.lead_id, row.id)
            interest = projection['expressed_commercial_interest']
            if not projection['conflict'] and projection['source_status'] == 'current':
                value = conversation(projection['effective_call_details'], row.outcome_code)
        if start <= row.occurred_at < end:
            counts[value] += 1
            interests[interest['value']] += 1
            reason = interest['reason']
            interest_reasons[reason] = interest_reasons.get(reason, 0) + 1
        if value == 'yes' and hot_start <= row.occurred_at < hot_end:
            company = index.companies[row.id]
            previous = hot.get(company)
            if previous is None or row.occurred_at.isoformat() > previous:
                hot[company] = row.occurred_at.isoformat()
    return {'definition': 'Human conversation with proven decision maker; buying intent not required.',
        'counts': counts, 'source': 'canonical human call details + source-digest-bound note facts',
        'hot_15d': {'start': hot_start.isoformat(), 'end_exclusive': hot_end.isoformat(),
            'company_count': len(hot), 'companies': [{'canonical_company_id': company,
                'last_conversation_at': hot[company]} for company in sorted(hot)],
            'qualification_or_contact_permission': False},
        'expressed_commercial_interest': interests,
        'interest_coverage': {'reasons': interest_reasons, 'fact_authority': False,
            'source': 'Human refusal or source-bound advisory interpretation; never inferred from conversation.'},
        'history_coverage': 'Use phone_attempts separately; first-ever is not implied.'}
