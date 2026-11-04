"""Authenticated read-only receipt projection. No raw notes, transport or writes."""
import math
import re

from sqlalchemy import select

from src.crm.persistence.models import AgentWork
from src.crm.services.jev_call_facts import VERSION, questions_for, supported_policy_versions, paired_evidence
from src.crm.services.jev_note_runtime import target_identity
from src.crm.services.jev_note_policy import digest
from src.crm.services.jev_note_worker import KIND, _current_request

REASONS = {'cited_interpretation', 'provider_unknown', 'value_confidence',
           'value_probability_or_margin', 'evidence_unknown', 'evidence_confidence',
           'evidence_probability_or_margin', 'evidence_value_mismatch', 'value_probability', 'evidence_probability'}


def _provider_projection(item, options, *, version):
    """Bounded categorical diagnostics only. Never copy provider prose/extra keys."""
    raw = item.get('provider_answers')
    if not isinstance(raw, dict) or set(raw) != {'value', 'evidence'}:
        return None
    result = {}
    for label in ('value', 'evidence'):
        answer = raw[label]
        if not isinstance(answer, dict) or set(answer) != {'choice', 'probabilities', 'confidence'}:
            return None
        probabilities = answer['probabilities']
        if not isinstance(probabilities, dict) or not probabilities or len(probabilities) > 61:
            return None
        keys = set(probabilities)
        if label == 'value':
            if keys != set(options):
                return None
        else:
            expected = {f's{i}__{value}' if paired_evidence(version) else f's{i}'
                        for i in range(12) for value in options if value != 'unknown'} | {'unknown'}
            if 'unknown' not in keys or not keys <= expected:
                return None
        choice, confidence = answer['choice'], answer['confidence']
        if (not isinstance(choice, str) or choice not in keys
                or any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1
                       for n in [confidence, *probabilities.values()])
                or abs(sum(probabilities.values()) - 1) > .01
                or probabilities[choice] != max(probabilities.values())):
            return None
        ranked = sorted(probabilities.values(), reverse=True)
        result[label] = {'choice': choice, 'probabilities': dict(probabilities), 'confidence': confidence,
                         'top_probability': ranked[0], 'margin': ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]}
    return result


def read_diagnostic(session, workspace_id, work_id, receipt_id):
    work = session.scalar(select(AgentWork).where(AgentWork.workspace_id == workspace_id,
        AgentWork.id == work_id, AgentWork.kind == 'call_followup'))
    receipt = session.scalar(select(AgentWork).where(AgentWork.workspace_id == workspace_id,
        AgentWork.id == receipt_id, AgentWork.kind == KIND))
    if work is None or receipt is None:
        raise LookupError('receipt_unavailable')
    result, payload = receipt.result or {}, receipt.payload or {}
    versions = supported_policy_versions()
    if (receipt.lead_id != work.lead_id or result.get('work_id') != str(work.id)
            or result.get('target') != target_identity(session)
            or result.get('simulated') is not False or result.get('version') not in versions
            or payload.get('flags') != versions[result['version']]
            or payload.get('event_id') != work.payload.get('activity_id')
            or payload.get('source_digest') != work.payload.get('source_digest')):
        raise LookupError('receipt_unavailable')
    try:
        _current_request(session, receipt)
        freshness = 'current'
    except (ValueError, KeyError, TypeError, StopIteration):
        freshness = 'stale_or_unsuccessful'
    decisions = {}
    for field, question in questions_for(payload['flags']).items():
        item = (result.get('decisions') or {}).get(field, {})
        confidence = item.get('confidence')
        if not (type(confidence) in (float, int) and math.isfinite(confidence) and 0 <= confidence <= 1):
            confidence = None
        value = item.get('value')
        if value not in question['criteria']:
            value = 'unknown'
        passage = item.get('passage')
        if not isinstance(passage, str) or not re.fullmatch(r's(?:[0-9]|1[01])', passage):
            passage = 'unknown'
        reason = item.get('reason')
        if reason not in REASONS:
            reason = 'value_confidence' if confidence is not None and confidence < .9 else 'legacy_gate_detail_unrecorded'
        decisions[field] = {'value': value, 'passage': passage, 'confidence': confidence,
                            'reason': reason, 'fact_authority': False}
        provider = _provider_projection(item, question['criteria'], version=result['version'])
        decisions[field]['provider_answers'] = provider
    status = result.get('status')
    if status not in ('pending', 'succeeded', 'failed', 'uncertain', 'fallback'):
        status = 'unavailable'
    failure = result.get('failure_detail')
    from src.crm.services.jev_note_worker import _FAILURE_CODES
    if not (isinstance(failure, dict) and (
            (set(failure) == {'kind', 'http_status'} and failure['kind'] == 'http_status'
             and type(failure['http_status']) is int and 100 <= failure['http_status'] <= 599)
            or (set(failure) == {'kind', 'code'} and failure['kind'] == 'validation' and isinstance(failure['code'], str) and failure['code'] in _FAILURE_CODES)
            or (set(failure) == {'kind'} and failure['kind'] in ('invalid_json', 'invalid_response')))):
        failure = None
    latency = result.get('latency_ms')
    if type(latency) is not int or not 0 <= latency <= 60_000:
        latency = None
    return {'receipt_id': str(receipt.id), 'work_id': str(work.id), 'policy_version': result['version'],
            'failure_detail': failure, 'latency_ms': latency,
            'receipt_result_hash': receipt.result_hash if receipt.result_hash == digest(result) else None,
            'status': status, 'freshness': freshness, 'decisions': decisions,
            'inference_performed': False, 'historical_receipt': True,
            'thresholds': {'confidence': None if result['version'] == VERSION else .9,
                           'top_probability': .9, 'margin': None if result['version'] == VERSION else .2},
            'limitation': 'Missing historical provider diagnostics remain null; confidence is distribution concentration, not correctness probability. No reconstruction or resend.'}
