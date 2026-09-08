"""Strict additive call fact shapes shared by HTTP and domain writers."""
from typing import Literal
from pydantic import AwareDatetime
from src.crm.domain.phone_proof_contract import EvidenceRef
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, Field, field_validator, model_validator


class CallDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    attempted: Literal[True]
    first_conversation: StrictBool | None = None
    # Operator assertion about ALL contact channels immediately before this call.
    # Missing history is unknown; independent from whether anyone answered.
    contact_kind: Literal["first_contact", "follow_up", "unknown"] = "unknown"
    answer_kind: Literal["unknown", "human_counterparty", "no_answer", "ivr", "voicemail", "wrong_number"]
    useful: StrictBool | None
    decision_maker: StrictBool | None
    interlocutor_role: Literal["unknown", "reception", "decision_maker", "other"]
    repeat_reason: StrictStr | None = Field(max_length=500)

    @field_validator("schema_version", "attempted", mode="before")
    @classmethod
    def exact_literals(cls, value, info):
        expected = int if info.field_name == "schema_version" else bool
        if type(value) is not expected:
            raise ValueError("Literal must have the exact type")
        return value

    @field_validator("repeat_reason")
    @classmethod
    def nonblank_reason(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("Reason must be nonblank")
        return value

    @model_validator(mode="after")
    def independent_dimensions(self):
        if self.first_conversation is True and self.answer_kind != "human_counterparty":
            raise ValueError("First conversation requires a human answer")
        if (self.useful is True or self.decision_maker is True) and self.answer_kind != "human_counterparty":
            raise ValueError("Useful or decision maker requires a human counterparty")
        if self.decision_maker is True and self.interlocutor_role != "decision_maker":
            raise ValueError("A decision maker requires the explicit role")
        return self

    def validate_outcome(self, outcome):
        if outcome in {"no_answer", "voicemail", "wrong_number"} and self.answer_kind not in {outcome, "unknown"}:
            raise ValueError("Contradictory call outcome")


class CallIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    purpose: Literal["internal_preparation", "agreed_callback"]
    agreed_with_client: StrictBool
    calendar_policy: Literal["none"]
    obligation_key: StrictStr = Field(min_length=1, max_length=256)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=8)
    gate_reason: StrictStr | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def consistent_intent(self):
        if self.agreed_with_client != (self.purpose == "agreed_callback"):
            raise ValueError("Callback requires explicit client agreement")
        return self


class PhoneHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    state: Literal["unknown", "complete_no_prior_answer", "prior_answer"]
    coverage_start_at: AwareDatetime | None
    covered_through_at: AwareDatetime | None
    evidence_refs: list[EvidenceRef] = Field(max_length=8)
    reason: StrictStr = Field(min_length=1, max_length=2000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("Version must be an integer")
        return value

    @model_validator(mode="after")
    def consistent_history(self):
        if self.reason != self.reason.strip() or not self.reason:
            raise ValueError("Reason must be nonblank")
        if self.state == "complete_no_prior_answer":
            if not self.coverage_start_at or not self.covered_through_at or self.coverage_start_at >= self.covered_through_at:
                raise ValueError("Complete baseline requires a coverage interval")
        elif self.coverage_start_at is not None or self.covered_through_at is not None:
            raise ValueError("Only baseline has coverage timestamps")
        return self
