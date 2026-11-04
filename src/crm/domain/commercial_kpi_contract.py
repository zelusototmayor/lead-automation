"""Separate, strict commercial-call KPI values; never extend legacy CallDetails."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)


def _canonical_uuid(value):
    if isinstance(value, str):
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError("UUID must be canonical")
        return parsed
    return value


CanonicalUUID = Annotated[UUID, BeforeValidator(_canonical_uuid)]

RULE = "commercial-call-kpis/v1"
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Counter = Annotated[int, Field(ge=0, le=9007199254740991)]
Relevance = Literal["yes", "no", "unknown"]
AttemptKind = Literal["new", "follow_up", "unknown"]
Exclusion = Literal[
    "non_phone",
    "test_scaffold",
    "voided",
    "cancelled_before_occurrence",
    "duplicate",
    "superseded",
]


class StrictValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def bounded_audit_details(value):
    """JSONB text uses comma/colon spaces and literal UTF-8; never truncate."""
    wire = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(", ", ": ")
    )
    if re.search(r"(?<!\\)(?:\\\\)*\\u0000", wire):
        raise ValueError("PostgreSQL JSONB cannot store NUL")
    if len(wire.encode("utf-8")) > 4096:
        raise ValueError("Audit details exceed 4096 UTF-8 bytes")
    return value


class EvidenceRef(StrictValue):
    activity_id: CanonicalUUID
    source_version: Hash
    field: Literal[
        "summary",
        "outcome_code",
        "call_details",
        "source_identity",
        "supersedes_activity_id",
    ]
    start: Counter | None
    end: Counter | None

    @model_validator(mode="after")
    def valid_span(self):
        if self.field == "summary":
            if self.start is None or self.end is None or self.start >= self.end:
                raise ValueError("Summary requires a nonempty codepoint range")
        elif self.start is not None or self.end is not None:
            raise ValueError("Only summary accepts spans")
        return self


class HistoryCoverage(StrictValue):
    state: Literal[
        "complete_no_prior_attempt",
        "prior_attempt_proved",
        "incomplete",
        "ambiguous_order",
    ]
    oldest_known_at: AwareDatetime | None
    proof_activity_id: CanonicalUUID | None


class CorrectionV1(StrictValue):
    relevant: Relevance
    phone_attempt_kind: AttemptKind
    eligibility: Literal["eligible", "excluded"]
    exclusion_reason: Exclusion | None
    reason: str = Field(min_length=1, max_length=240)
    evidence_refs: list[EvidenceRef] = Field(max_length=4)
    history_coverage: HistoryCoverage


class ProposalV1(CorrectionV1):
    rule_version: Literal["commercial-call-kpis/v1"]
    activity_id: CanonicalUUID


class AssessmentV1(StrictValue):
    rule_version: Literal["commercial-call-kpis/v1"]
    activity_id: CanonicalUUID
    canonical_company_id: str = Field(
        pattern=r"^(account|lead):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    source_digest: Hash
    source_version: Hash
    context_digest: Hash
    relevant: Relevance
    phone_attempt_kind: AttemptKind
    eligibility: Literal["eligible", "excluded"]
    exclusion_reason: Exclusion | None
    reason: str = Field(min_length=1, max_length=240)
    evidence_refs: list[EvidenceRef] = Field(max_length=4)
    provenance: Literal["human", "inferred"]
    assessed_at: AwareDatetime
    freshness: Literal["current", "pending", "stale", "conflict"]
    history_coverage: HistoryCoverage

    @model_validator(mode="after")
    def consistent_claims(self):
        if self.source_version != self.source_digest:
            raise ValueError("Source version must be content addressed")
        if not self.reason.strip():
            raise ValueError("Reason must be nonblank")
        if (self.eligibility == "excluded") != (self.exclusion_reason is not None):
            raise ValueError("Exclusion reason must match eligibility")
        if (
            self.relevant != "unknown"
            or self.phone_attempt_kind != "unknown"
            or self.eligibility == "excluded"
        ) and not self.evidence_refs:
            raise ValueError("Claims require evidence")
        if (
            self.phone_attempt_kind == "new"
            and self.history_coverage.state != "complete_no_prior_attempt"
        ):
            raise ValueError("New requires affirmative phone history completeness")
        if self.phone_attempt_kind == "follow_up" and (
            self.history_coverage.state != "prior_attempt_proved"
            or self.history_coverage.proof_activity_id is None
        ):
            raise ValueError("Follow-up requires a prior attempt proof")
        return self
