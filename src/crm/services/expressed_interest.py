"""Read-only, source-bound buying-interest evidence, independent of conversation.

Human call outcomes take precedence. Model interpretations remain advisory and
cannot qualify a company, change CallDetails or authorize any contact.
"""
import math

from src.crm.services.jev_call_facts import VERSION, V2_VERSION, V3_VERSION, passages, paired_evidence


def unknown(reason):
    return {'value': 'unknown', 'reason': reason, 'confidence': None,
        'provenance': None, 'fact_authority': False, 'qualification_or_contact_permission': False}


def read_interest(session, source, candidates, *, superseded=False):
    if superseded:
        return unknown('superseded_source')
    if source.outcome_code == 'not_interested':
        return dict(unknown('human_outcome'), value='no', provenance='human_outcome')
    if (source.outcome_code in ('no_answer', 'voicemail', 'wrong_number')
            or (source.call_details or {}).get('answer_kind') in ('no_answer', 'ivr', 'voicemail', 'wrong_number')):
        return unknown('no_human_exchange')
    accepted = []
    for item in candidates:
        if (not isinstance(item, dict) or item.get('policy_version') not in (V2_VERSION, V3_VERSION, VERSION)
                or item.get('fact_authority') is not False
                or item.get('value') not in ('yes', 'no', 'unknown')):
            return unknown('conflicting_interpretation')
        if item['value'] == 'unknown':
            continue
        try:
            quotes = passages({'activity_type': source.activity_type, 'summary': source.summary},
                              expanded=paired_evidence(item['policy_version']))
        except ValueError:
            return unknown('invalid_cited_interpretation')
        confidence = item.get('confidence')
        if (type(confidence) not in (float, int) or not math.isfinite(confidence)
                or not (0 if item['policy_version'] == VERSION else .9) <= confidence <= 1 or not item.get('quote')
                or quotes.get(item.get('passage')) != item['quote']):
            return unknown('invalid_cited_interpretation')
        if item['policy_version'] == VERSION and any(
                type(item.get(key)) not in (float, int) or not math.isfinite(item[key])
                or not .9 <= item[key] <= 1
                for key in ('selected_probability', 'evidence_selected_probability')):
            return unknown('invalid_cited_interpretation')
        accepted.append(item)
    if len({item['value'] for item in accepted}) > 1:
        return unknown('conflicting_interpretation')
    if accepted:
        item = min(accepted, key=lambda item: item['confidence'])
        return item | {'qualification_or_contact_permission': False}
    if candidates:
        reason = candidates[0].get('reason', 'classified_unknown')
        return unknown(reason)
    # Preserve the pre-existing separate advisory policy when already applied;
    # read its canonical freshness checks without issuing another inference.
    from src.crm.services.jev_note_worker import _projection
    hints, lineage = _projection(session, source.workspace_id, source.lead_id, source.id)
    hint = hints.get('interest')
    if hint and hint.get('value') in ('yes', 'no', 'unknown'):
        return dict(unknown(hint.get('reason', 'source_bound_advisory')),
            value=hint['value'], confidence=hint.get('confidence'),
            provenance='source_bound_advisory', receipt_id=lineage.get('interest'))
    return unknown('not_captured_by_call_facts_v1')
