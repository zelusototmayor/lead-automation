"""First human conversation != first phone attempt; legacy KPI stays unchanged."""
from datetime import datetime
from uuid import UUID


def first_conversation_state(context, *, index):
    facts = context.get('call_details') or {}
    if facts.get('first_conversation') is False:
        return 'follow_up'
    if facts.get('first_conversation') is not True or facts.get('answer_kind') != 'human_counterparty':
        return 'unknown'
    occurred = datetime.fromisoformat(context['occurred_at'])
    uncertain = False
    for prior in context['history']:
        prior_id = UUID(prior['activity_id'])
        # Reuse the aggregate's canonical snapshot without changing source or
        # assessment digests. Versions/aliases are not separate conversations.
        if prior_id in index.exclusions:
            continue
        if prior['activity_id'] == context['activity_id']:
            if prior.get('actor_type') != 'human' or prior_id in index.conflicts:
                return 'unknown'
            continue
        if prior.get('actor_type') in ('import', 'migration', 'system'):
            continue  # Recording time is not an observed conversation date.
        if datetime.fromisoformat(prior['occurred_at']) > occurred:
            continue
        if prior_id in index.conflicts:
            uncertain = True
            continue
        details = prior.get('call_details') or {}
        # A contradicted declaration is not counted; absence remains unknown.
        if details.get('answer_kind') == 'human_counterparty' or prior.get('outcome_code') in ('connected','not_interested','follow_up'):
            return 'conflict'
        if datetime.fromisoformat(prior['occurred_at']) == occurred:
            uncertain = True
    return 'unknown' if uncertain else 'new'
