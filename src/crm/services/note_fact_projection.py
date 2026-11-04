"""Order-independent union of immutable, exact-source factual interpretations.

An abstention is not a contrary observation. Compatible partial interpretations
can enrich each other; any actual disagreement or malformed evidence invalidates
the whole projection. Human values and cross-field constraints are applied by
readers after merging. Audit rows are never rewritten or selected last-wins.
"""
from pydantic import TypeAdapter, ValidationError

from src.crm.domain.call_contract import CallDetails
from src.crm.services.jev_note_policy import digest

WITHDRAWAL_ACTION = 'agent.note_fact_withdrawn'


def reviewed_candidates(audits, withdrawals):
    """Apply exact, append-only withdrawals; never rank models or older audits."""
    rows = {str(row.id): row for row in audits}
    removed, reviews = {}, []
    for review in withdrawals:
        data = review.details or {}
        target, support = rows.get(data.get('target_audit_id')), rows.get(data.get('supporting_audit_id'))
        if (review.action != WITHDRAWAL_ACTION or review.entity_type != 'activity' or data.get('version') != 'note-fact-withdrawal/v1'
                or data.get('status') != 'reviewed_adjudication'
                or data.get('reason_code') != 'semantic_model_error'
                or data.get('field') != 'answer_kind' or data.get('withdrawn_value') != 'no_answer'
                or target is None or support is None or target.id == support.id
                or any(row.workspace_id != review.workspace_id or row.entity_id != review.entity_id
                       or row.entity_type != 'activity' or row.action != 'agent.note_interpreted'
                       for row in (target, support))
                or not data.get('work_id')
                or any((row.details or {}).get('work_id') != data['work_id'] for row in (target, support))
                or any((row.details or {}).get('source_digest') != data.get('source_digest') for row in (target, support))
                or digest(target.details) != data.get('target_audit_payload_digest')
                or digest(support.details) != data.get('supporting_audit_payload_digest')
                or (target.details.get('facts') or {}).get('answer_kind') != 'no_answer'
                or (support.details.get('facts') or {}).get('answer_kind') != 'human_counterparty'
                or not (target.details.get('jev') or {}).get('receipt_id')):
            continue
        removed.setdefault(str(target.id), set()).add('answer_kind')
        reviews.append({'audit_id': str(review.id), 'target_audit_id': str(target.id),
            'supporting_audit_id': str(support.id), 'field': 'answer_kind',
            'status': 'reviewed_adjudication', 'reason_code': 'semantic_model_error',
            'source_digest': data['source_digest'], 'review_ref': data.get('review_ref'),
            'quote': data.get('quote'), 'audit_payload_digest': digest(data), 'inference_performed': False})
    candidates = [({key: value for key, value in row.details.get('facts', {}).items()
                    if key not in removed.get(str(row.id), set())}
                   if isinstance((row.details or {}).get('facts', {}), dict) else row.details['facts'])
                  for row in audits]
    return candidates, removed, reviews

FIELDS = frozenset({'answer_kind', 'useful', 'decision_maker', 'interlocutor_role', 'repeat_reason'})
_ADAPTERS = {name: TypeAdapter(CallDetails.model_fields[name].rebuild_annotation()) for name in FIELDS}


def merge_facts(candidates):
    merged = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or not set(candidate) <= FIELDS:
            return {}, True
        for field, value in candidate.items():
            if value is None or value == 'unknown':
                continue
            try:
                _ADAPTERS[field].validate_python(value, strict=True)
            except ValidationError:
                return {}, True
            if field in merged and (type(merged[field]) is not type(value) or merged[field] != value):
                return {}, True
            merged[field] = value
    return merged, False
