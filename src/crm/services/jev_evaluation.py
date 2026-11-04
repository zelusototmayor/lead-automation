"""Measurement and explicit promotion configuration, never autonomous promotion.

Reports are observations, not calibrated win rates or evidence of provider
quality. Token usage/cost belong to a grouped request, not each point. Real
promotion references must be supplied by the trusted operator after review;
synthetic exercises of this contract are NOT those approvals.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from src.crm.services.typed_decisions import (
    CRM_QUESTIONS, MODEL, VERSION, _bounded_number, build_request,
)


@dataclass(frozen=True)
class PromotionGate:
    enabled: bool = False
    points: tuple[str, ...] = ()
    review_ref: str = ''
    privacy_ref: str = ''
    owner_ref: str = ''
    evidence_ref: str = ''
    simulated_evidence: bool = True
    version: str = VERSION
    model: str = MODEL

    def allows(self, requested):
        return (self.enabled is True and self.simulated_evidence is False
                and self.version == VERSION and self.model == MODEL
                and bool(requested) and set(requested) <= set(self.points) <= set(CRM_QUESTIONS)
                and all(isinstance(ref, str) and 0 < len(ref) <= 128 for ref in (
                    self.review_ref, self.privacy_ref, self.owner_ref, self.evidence_ref)))


def current_hints(receipt, source, *, workspace_id, score_questions=None):
    """Read-only sidecar: source edits/supersession invalidate even a success.

    These hints may only be displayed separately from human facts. No mutator
    is exposed, and copying prevents a consumer from mutating the receipt.
    """
    if (receipt.get('status') != 'succeeded' or source.get('status') != 'current'
            or source.get('activity_id') != receipt.get('event_id')
            or source.get('source_digest') != receipt.get('correlation_id')):
        return {}
    try:
        current = build_request(source, workspace_id=workspace_id, flags=receipt.get('flags'),
                                score_questions=score_questions)
    except (ValueError, TypeError):
        return {}
    if current['decision_id'] != receipt.get('decision_id'):
        return {}  # Tenant, contract, questions or source context changed.
    return deepcopy(receipt['decisions'])


def observations(receipt, *, labels=None, baseline=None):
    """Allowlisted JSON export. Unknown/missing labels never imply correctness.

    Per-point latency references the shared request and MUST NOT be summed.
    No event/company/contact/name/note or provider error text is exported.
    Labels/baselines are explicit closed choices or bounded rubric scores from
    the evaluator, never copied from confidence. Rubric text is not exported.
    """
    labels, baseline = labels or {}, baseline or {}
    contracts = receipt.get('score_contracts', {})
    for values in (labels, baseline):
        for point, value in values.items():
            if point in contracts:
                valid = value == 'unknown' or _bounded_number(value, contracts[point]['level_count'] - 1)
            else:
                valid = (point in CRM_QUESTIONS and isinstance(value, str)
                         and value in CRM_QUESTIONS[point]['criteria'])
            if not valid:
                raise ValueError('evaluation_label_invalid')
    rows = []
    for point, mode in receipt['flags'].items():
        if mode == 'off':
            continue
        decision = receipt['decisions'].get(point, {})
        predicted = decision.get('value')
        question_type = 'score' if point in contracts else 'choice'
        kind = question_type if predicted is not None and predicted != 'unknown' else 'unknown'
        label = labels.get(point)
        baseline_value = baseline.get(point)
        score_label_known = question_type != 'score' or label not in (None, 'unknown')
        rows.append({'point': point, 'mode': mode, 'prediction': predicted,
            'type': question_type, 'prediction_kind': kind,
            'confidence': decision.get('confidence'), 'fact_authority': False,
            'label': label, 'baseline': baseline_value,
            'matches_label': predicted == label
                if predicted is not None and label is not None and score_label_known
                and not (question_type == 'score' and kind == 'unknown') else None,
            'baseline_matches_label': baseline_value == label
                if baseline_value is not None and label is not None and score_label_known
                and not (question_type == 'score' and baseline_value == 'unknown') else None,
            'fallback': receipt['status'] != 'succeeded' or kind == 'unknown'})
    extension = {'score_contracts': deepcopy(contracts)} if contracts else {}
    return {'schema': 'crm-jev-observations/v2', 'request': {
        'receipt_id': receipt['receipt_id'], 'model': receipt['model'], 'version': receipt['version'],
        'status': receipt['status'], 'simulated': receipt['simulated'], 'usage': deepcopy(receipt['usage']),
        'latency_ms': receipt['latency_ms'], 'actual_cost_eur': receipt['actual_cost_eur'],
        'cost_status': receipt['cost_status'], **extension}, 'points': rows,
        'quality_claim': 'none; observations only', 'automatic_promotion': False}
