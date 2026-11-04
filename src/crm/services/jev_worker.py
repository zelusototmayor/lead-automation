"""Optional post-commit Jev shadow worker. Never a CRM/Calendar/mail writer.

The consumer explicitly awaits this in a SEPARATE worker, not prepare_post_call.
It returns a sidecar hint envelope, never patches a projection. No automatic
retry, no exactly-once external claim. All flags default OFF. Real use requires
an explicit code/privacy review and a request reservation from Main's ledger.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
import time


import httpx

from src.crm.services.typed_decisions import MODEL, build_request, validate_answers
from src.crm.services.jev_evaluation import PromotionGate

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
MAX_REQUEST_BYTES = 24_000
MAX_RESPONSE_BYTES = 64_000


@dataclass(frozen=True)
class LivePermit:
    """References only. Trusted caller must obtain a per-request reservation.

    Not a replacement for the global EUR20 ledger; local flags create no budget.
    """
    review_ref: str = ''
    privacy_ref: str = ''
    reservation_ref: str = ''

    def valid(self):
        return all(isinstance(v, str) and 0 < len(v) <= 128
                   for v in (self.review_ref, self.privacy_ref, self.reservation_ref))


@dataclass
class JevHTTP:
    mock_transport: httpx.MockTransport | None = field(default=None, repr=False)
    credential: str | None = field(default=None, repr=False)
    permit: LivePermit = field(default_factory=LivePermit)
    deadline_seconds: float = 2.0

    @property
    def simulated(self):
        return isinstance(self.mock_transport, httpx.MockTransport)

    def preflight(self):
        if not 0 < self.deadline_seconds <= 10:
            raise ValueError('deadline_invalid')
        if self.mock_transport is not None and not self.simulated:
            raise ValueError('mock_transport_required')
        if not self.simulated and (not self.permit.valid() or self.credential is None):
            raise ValueError('live_gates_missing')
        if not self.simulated and (not isinstance(self.credential, str)
                or not self.credential or '\r' in self.credential or '\n' in self.credential):
            raise ValueError('credential_invalid')

    async def evaluate(self, body):
        self.preflight()
        payload = json.dumps(body, allow_nan=False, separators=(',', ':')).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError('request_limit')
        headers = {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
        if not self.simulated:
            # The credential is runtime-only: never persisted, logged or exported.
            headers['Authorization'] = 'Bearer ' + str(self.credential)
        transport = self.mock_transport or httpx.AsyncHTTPTransport(retries=0)
        async with asyncio.timeout(self.deadline_seconds):
            async with httpx.AsyncClient(transport=transport, trust_env=False,
                    follow_redirects=False, timeout=self.deadline_seconds) as client:
                async with client.stream('POST', ENDPOINT, content=payload, headers=headers) as response:
                    response.raise_for_status()
                    if response.headers.get('content-encoding', 'identity') != 'identity':
                        raise ValueError('encoded_response_rejected')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ValueError('response_limit')
                    return json.loads(data)


def _envelope(request, simulated):
    receipt_id = hashlib.sha256((request['decision_id'] + ':' +
                                ('simulated' if simulated else 'provider')).encode()).hexdigest()
    extension = {'score_contracts': request['score_contracts']} if 'score_contracts' in request else {}
    return {key: request[key] for key in ('decision_id', 'event_id', 'correlation_id', 'version')} | extension | {
        'receipt_id': receipt_id, 'model': MODEL, 'status': 'disabled', 'decisions': {},
        'flags': request['flags'], 'fact_authority': False, 'simulated': simulated,
        'usage': None, 'actual_cost_eur': None, 'cost_status': 'simulated_not_billed' if simulated else 'unreconciled',
        'latency_ms': 0, 'error': None}


async def evaluate_shadow(source, *, workspace_id, store, http, flags=None, promotion=None,
                          score_questions=None):
    """Evaluate one current source after commit; replay returns the durable receipt.

    Store failures before submission fail closed; after submission, an existing
    pending receipt will expire to uncertain. No error contains provider text.
    """
    try:
        request = build_request(source, workspace_id=workspace_id, flags=flags,
                                score_questions=score_questions)
    except (ValueError, TypeError):
        return {'status': 'fallback', 'error': 'invalid_source_or_flags', 'decisions': {}, 'fact_authority': False}
    result = _envelope(request, http.simulated)
    if not request['body']['questions']:
        return result
    canary = {point for point, mode in request['flags'].items() if mode == 'canary'}
    if canary and (not isinstance(promotion, PromotionGate) or not promotion.allows(canary)):
        return dict(result, status='fallback', error='promotion_required')
    try:
        http.preflight()
    except ValueError:
        return dict(result, status='fallback', error='transport_gates')
    try:
        owner, receipt = store.claim(result, deadline_seconds=http.deadline_seconds)
    except sqlite3.Error:
        return dict(result, status='fallback', error='receipt_unavailable')
    if owner is None:
        return receipt
    started = time.monotonic()
    try:
        response = await http.evaluate(request['body'])
        decisions = validate_answers(response, request['body']['questions'])
        for point, decision in decisions.items():
            reason = ('score_hint' if decision.get('type') == 'score'
                      and decision['value'] != 'unknown' else decision['value'])
            decision['reason_code'] = point + ':' + reason
            decision['evidence_refs'] = [{'event_id': request['event_id'],
                'source_digest': request['correlation_id'], 'support': 'unverified_hint'}]
        usage = response.get('usage')
        if (not isinstance(usage, dict) or set(usage) != {'input_tokens', 'output_tokens'}
                or any(type(v) is not int or not 0 <= v <= 1_000_000_000 for v in usage.values())):
            raise ValueError('usage_invalid')
        result.update(status='succeeded', decisions=decisions, usage=usage)
    except asyncio.CancelledError:
        try:
            store.finish(result['receipt_id'], owner, dict(result, status='uncertain', error='cancelled'))
        except sqlite3.Error:
            pass  # Durable pending intent still prevents automatic resend.
        raise
    except (TimeoutError, httpx.RequestError):
        result.update(status='uncertain', error='transport_outcome_unknown')
    except httpx.HTTPStatusError:
        result.update(status='failed', error='http_status')
    except (ValueError, TypeError):
        result.update(status='failed', error='schema_or_limits')
    except Exception:
        result.update(status='uncertain', error='worker_outcome_unknown')
    result['latency_ms'] = round((time.monotonic() - started) * 1000)
    try:
        return store.finish(result['receipt_id'], owner, result)
    except sqlite3.Error:
        return dict(result, status='uncertain', error='receipt_commit_unknown', decisions={})


async def evaluate_post_call(payload, *, workspace_id, store, http, flags=None, promotion=None,
                             score_questions=None):
    """Existing AgentWork.payload.post_call seam, consumed only AFTER commit.

    No worker/timer is activated by importing this function. The future adopting
    consumer must re-read note_context under existing workspace authorization,
    pass that current snapshot, and use current_hints on a fresh read before
    displaying a result. This function never replaces any field of the payload.
    """
    source = payload.get('source') if isinstance(payload, dict) else None
    if (not isinstance(source, dict) or payload.get('version') != 'post-call/v1'
            or payload.get('event_id') != source.get('activity_id')
            or payload.get('correlation_id') != source.get('source_digest')):
        return {'status': 'fallback', 'error': 'post_call_source_conflict',
                'decisions': {}, 'fact_authority': False}
    return await evaluate_shadow(source, workspace_id=workspace_id, store=store,
                                 http=http, flags=flags, promotion=promotion,
                                 score_questions=score_questions)
