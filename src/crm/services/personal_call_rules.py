"""José's explicit call convention, separate from immutable model receipts.

This is a prospective business-rule projection, not a replacement classifier.
Only genuine manually saved call/note entries after the configured activation
instant are eligible. A model still interprets decision-maker context; it may
not turn missing attendance evidence into an unanswered or non-call entry.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import os
import re

VERSION = 'jose-call-convention/2026-09-30-v2'
ACTIVATION_ENV = 'CRM_PERSONAL_CALL_RULES_FROM'
OWNER_EVIDENCE_VERSION = 'owner-call-evidence/2026-09-30-v1'

# Deliberately narrow, observable source phrases. These are not a replacement
# keyword classifier. An unqualified negative must describe this interaction;
# a manager being absent or asking to call later never counts as no answer.
_CLAUSE = r'(?:^|[.;!\n]\s*)'
_END = r'\s*(?=$|[.,;!:—–\n-])'
_NOT_CALL = re.compile(
    _CLAUSE + r'\s*(?:not a call|n[aã]o (?:foi|[eé]) (?:uma )?chamada|'
    r'(?:isto [eé] )?(?:s[oó]|apenas) uma nota|n[aã]o liguei)' + _END,
    re.IGNORECASE,
)
_PRESENT_PREFIX = r'(?:(?:hoje|agora|ainda)\s+){0,3}'
_NO_ANSWER = re.compile(
    _CLAUSE + r'\s*' + _PRESENT_PREFIX + r'(?:(?:liguei|tentei ligar)(?:\s+(?:mas|e))?\s*[,—–-]?\s*)?'
    + _PRESENT_PREFIX + r'(?:ningu[eé]m atendeu|n[aã]o (?:me )?atend(?:eu|eram)|sem resposta|sem atendimento|'
    r'chamada n[aã]o atendida|(?:foi|caiu|fui) (?:diret[oa] )?(?:para (?:a )?|na )'
    r'(?:caixa (?:de (?:voz|mensagens)|postal)|voicemail)|no answer)' + _END,
    re.IGNORECASE,
)
# An explicitly historical conversation is not evidence that today's call
# was answered. General ambiguous wording is still interpreted by the model.
_HUMAN_CONVERSATION = re.compile(
    r'(?<!j[aá] )(?<!ontem )(?<!antes )\b(?:falei com|fal[aá]mos|conversei com|disse-me|disse me|disseram-me|'
    r'(?:rece[cp]cionista|funcion[aá]ri[oa]) (?:disse|indicou|atendeu|pediu))\b',
    re.IGNORECASE,
)


def activation_time():
    value = os.environ.get(ACTIVATION_ENV)
    if not value:
        return None
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('personal_call_rules_activation_requires_timezone')
    return instant.astimezone(UTC)


def eligible(source, *, activated_at=None):
    activated_at = activated_at if activated_at is not None else activation_time()
    created_at = getattr(source, 'created_at', None)
    return bool(activated_at is not None and created_at is not None
        and created_at.tzinfo is not None and created_at >= activated_at
        and source.actor_type == 'human' and source.source_system == 'manual'
        and (source.activity_type == 'call'
             or (source.activity_type == 'note' and source.title == 'Note added')))


def _evidence(match):
    if match is None:
        return None
    return {'start': match.start(), 'end': match.end(), 'text': match.group(0)}


def interaction(source):
    """Return explicit source evidence plus the owner's default; never unknown."""
    text = source.summary or ''
    not_call = _NOT_CALL.search(text)
    if not_call:
        return {'call': False, 'answered': False, 'reason': 'explicit_not_a_call',
            'evidence': _evidence(not_call)}
    details = source.call_details or {}
    if source.outcome_code in ('no_answer', 'voicemail') or details.get('answer_kind') in (
            'no_answer', 'voicemail', 'ivr_only'):
        return {'call': True, 'answered': False, 'reason': 'explicit_structured_no_answer',
            'evidence': {'outcome_code': source.outcome_code,
                'answer_kind': details.get('answer_kind')}}
    no_answer = _NO_ANSWER.search(text)
    # "Falei com a rececionista. O Rui não atendeu" is an answered call.
    # Explicit UI no-answer above remains authoritative if the operator sets it.
    if no_answer and not _HUMAN_CONVERSATION.search(text):
        return {'call': True, 'answered': False, 'reason': 'explicit_no_answer_text',
            'evidence': _evidence(no_answer)}
    return {'call': True, 'answered': True, 'reason': 'owner_attended_unless_explicit_no_answer',
        'evidence': None}


def _fact(value, reason):
    return {'value': value, 'policy_version': VERSION, 'reason': reason,
        'authority': 'owner_business_rule', 'model_inference': False}


def _semantic_interaction(source, classification):
    """Read only additional evidence sealed into an already verified receipt.

    The model supplies semantics, while the exact quote supplies source
    provenance. Older categorical answers contain no such evidence and cannot
    override the owner's attended-by-default convention.
    """
    if not classification or classification.get('owner_call_evidence_version') != OWNER_EVIDENCE_VERSION:
        return None
    evidence=classification.get('owner_call_evidence') or {}
    kind=evidence.get('kind'); quote=evidence.get('quote')
    if kind is None:
        return None
    text=source.summary or ''
    prediction=classification.get('prediction') or {}
    if (kind not in ('no_answer','not_a_call') or not isinstance(quote,str)
            or not quote.strip() or quote not in text or prediction.get('answered')!='no'):
        raise ValueError('personal_call_model_evidence_changed')
    start=text.index(quote)
    return {'call':kind!='not_a_call','answered':False,
        'reason':'model_explicit_'+kind,
        'evidence':{'start':start,'end':start+len(quote),'text':quote,
            'authority':'source_bound_model_evidence','version':OWNER_EVIDENCE_VERSION}}


def project(source, classification, *, activated_at=None):
    """Derive the effective read result while leaving receipt/source untouched.

    Each genuine newly saved entry is one interaction. Identical notes saved by
    distinct commands remain distinct attempts; source identity is preserved.
    """
    if not eligible(source, activated_at=activated_at):
        return classification
    effective = interaction(source)
    semantic = _semantic_interaction(source,classification)
    # Explicit not-a-call, including contextual wording the narrow immediate
    # parser cannot recognize, takes precedence. A semantic no-answer may only
    # replace the attended default, never an explicit not-a-call marker.
    if semantic and (not semantic['call'] or
            effective['reason']=='owner_attended_unless_explicit_no_answer'):
        effective=semantic
    original = deepcopy(classification) if classification is not None else {}
    units = original.get('units')
    classified = (units[0]['classification'] if units and len(units) == 1
        else original if 'call' in original else {})
    projected = deepcopy(classified)
    reason = effective['reason']
    for key in ('call', 'answered'):
        projected[key] = _fact(effective[key], reason)
        if reason.startswith('model_explicit_'):
            projected[key].update(authority='source_bound_model_evidence', model_inference=True)
    original_role = (classified.get('role') or {}).get('value')
    is_dm = effective['answered'] and original_role == 'decision_maker'
    role = ('decision_maker' if is_dm else
        original_role if effective['answered'] and original_role == 'gatekeeper' else
        'not_decision_maker' if effective['answered'] else 'not_applicable')
    projected['role'] = (deepcopy(classified['role']) if is_dm or role == 'gatekeeper'
        else _fact(role, 'no_supported_decision_maker_conversation'))
    projected['relevant'] = _fact(bool(is_dm), 'answered_decision_maker_conversation')
    for key in ('meeting', 'callback'):
        projected.setdefault(key, _fact('no', 'no_classification_yet'))
    binding = {'parent_activity_id': str(source.id), 'start': 0,
        'end': len(source.summary or ''), 'kind': 'entry', 'date_literal': None}
    original['units'] = [{'unit': binding, 'classification': projected}]
    original['business_rule_projection'] = {'version': VERSION,
        'authority': 'owner_business_rule', 'reason': reason,
        'evidence': effective['evidence'], 'source_activity_id': str(source.id),
        'receipt_unchanged': True,
        'decision_maker_status': ('classified' if original_role in ('decision_maker', 'gatekeeper')
            else 'pending_interpretation')}
    return original
