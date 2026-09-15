"""Strict, source-addressed KPI wire contract (independent of CallDetails)."""

import json
from uuid import uuid4

import pytest


def test_audit_envelope_rejects_postgres_utf8_overflow_without_truncation():
    from src.crm.domain.commercial_kpi_contract import bounded_audit_details

    ordinary = {"reason": "é" * 240, "refs": []}
    assert bounded_audit_details(ordinary) is ordinary
    with pytest.raises(ValueError, match="4096"):
        bounded_audit_details({"reason": "🧪" * 1100})
    with pytest.raises(ValueError):
        bounded_audit_details({"reason": "nul\u0000inside"})


from pydantic import ValidationError


def assessment_payload():
    activity = str(uuid4())
    return {
        "rule_version": "commercial-call-kpis/v1",
        "activity_id": activity,
        "canonical_company_id": "lead:" + str(uuid4()),
        "source_digest": "a" * 64,
        "source_version": "a" * 64,
        "context_digest": "b" * 64,
        "relevant": "unknown",
        "phone_attempt_kind": "unknown",
        "eligibility": "eligible",
        "exclusion_reason": None,
        "reason": "Evidence is incomplete",
        "evidence_refs": [],
        "provenance": "inferred",
        "assessed_at": "2026-09-15T12:00:00Z",
        "freshness": "current",
        "history_coverage": {
            "state": "incomplete",
            "oldest_known_at": None,
            "proof_activity_id": None,
        },
    }


def test_assessment_accepts_exact_versioned_unknown_not_legacy_extensions():
    from src.crm.domain.commercial_kpi_contract import AssessmentV1

    value = assessment_payload()
    assert (
        AssessmentV1.model_validate_json(json.dumps(value)).model_dump(mode="json")
        == value
    )
    with pytest.raises(ValidationError):
        AssessmentV1.model_validate_json(json.dumps({**value, "useful": True}))


@pytest.mark.parametrize(
    "change",
    [
        {"source_version": "c" * 64},
        {"reason": "   "},
        {"relevant": "yes"},
        {"relevant": "no"},
        {"phone_attempt_kind": "new"},
        {"phone_attempt_kind": "follow_up"},
        {"eligibility": "excluded"},
        {"exclusion_reason": "duplicate"},
        {"activity_id": "ABCDABCD-ABCD-ABCD-ABCD-ABCDABCDABCD"},
    ],
)
def test_assessment_rejects_inconsistent_claims(change):
    from src.crm.domain.commercial_kpi_contract import AssessmentV1

    with pytest.raises(ValidationError):
        AssessmentV1.model_validate_json(json.dumps({**assessment_payload(), **change}))


@pytest.mark.parametrize("span", [(True, 2), ("0", 2), (0, 0), (2, 1), (None, 1)])
def test_summary_span_is_strict_nonempty_unicode_range(span):
    from src.crm.domain.commercial_kpi_contract import EvidenceRef

    ref = {
        "activity_id": str(uuid4()),
        "source_version": "a" * 64,
        "field": "summary",
        "start": span[0],
        "end": span[1],
    }
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate_json(json.dumps(ref))
