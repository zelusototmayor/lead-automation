"""Explicit v3→v4 interpretation of an immutable response; never inference.

Only the canonical note writer persists the resulting projection. Source work,
provider receipt, human facts, prior audits and budget remain unchanged.
"""
from copy import deepcopy
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.crm.services.agent_work_service import WorkConflict
from src.crm.services.jev_note_policy import digest

_RECALIBRATION_AUTHORITY = object()
TARGET_POLICY = 'crm-call-facts/v4'


class RecalibrationBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: UUID
    jev_receipt_id: UUID
    expected_lead_version: int = Field(ge=1, strict=True)
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    review_ref: str = Field(min_length=1, max_length=48)
    target_policy: Literal['crm-call-facts/v4']


def prepare(session, receipt, body):
    """Called only after canonical work/company/source/receipt binding locks."""
    from src.crm.services import jev_call_facts as facts
    from src.crm.services.jev_note_worker import _current_request, load_snapshot
    if not isinstance(body, RecalibrationBody) or facts.VERSION != TARGET_POLICY:
        raise WorkConflict('Unsupported recalibration')
    result = receipt.result or {}
    if (result.get('version') != facts.V3_VERSION or result.get('status') != 'succeeded'
            or result.get('simulated') is not False
            or receipt.payload.get('flags') != facts.V3_FLAGS
            or receipt.result_hash != digest(result)):
        raise WorkConflict('Immutable v3 response required')
    original = _current_request(session, receipt)
    snapshot = load_snapshot(session, receipt.workspace_id, receipt.lead_id, {
        'activity_id': receipt.payload['event_id'], 'source_digest': receipt.payload['source_digest']})
    current = facts.build_request(snapshot, flags=facts.FLAGS)
    if original['body'] != current['body'] or original['snapshot_digest'] != current['snapshot_digest']:
        raise WorkConflict('Inference contract changed')
    stored = result.get('decisions')
    if not isinstance(stored, dict) or set(stored) != set(facts.questions_for(facts.V3_FLAGS)):
        raise WorkConflict('Complete stored response required')
    answers = {}
    for field, decision in stored.items():
        if not isinstance(decision, dict):
            raise WorkConflict('Complete stored response required')
        raw = decision.get('provider_answers')
        if not isinstance(raw, dict) or set(raw) != {'value', 'evidence'}:
            raise WorkConflict('Complete stored response required')
        for label, key in (('value', field), ('evidence', 'evidence_' + field)):
            value = raw[label]
            if not isinstance(value, dict) or set(value) != {'choice','probabilities','confidence'}:
                raise WorkConflict('Complete stored response required')
            answers[key] = {'type': 'choice', **deepcopy(value)}
    # map_response validates all fixed choices, probabilities and model pin.
    decisions = facts.map_response(current, {'model': result['model'], 'answers': answers})
    return decisions, {'receipt_result_hash': receipt.result_hash,
        'review_ref': body.review_ref,
        'inference_performed': False}


def recalibrate(session, principal, work_id, body):
    from src.crm.services.note_plan_service import apply_note_plan
    if not isinstance(body, RecalibrationBody):
        raise WorkConflict('Explicit recalibration command required')
    return apply_note_plan(session, principal, work_id, body, projection_only=True,
                           _recalibration=_RECALIBRATION_AUTHORITY)
