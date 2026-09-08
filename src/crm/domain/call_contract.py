"""Strict additive call fact shapes shared by HTTP and domain writers."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, Field, field_validator, model_validator


class CallDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    attempted: Literal[True]
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
        if (self.useful is True or self.decision_maker is True) and self.answer_kind != "human_counterparty":
            raise ValueError("Useful or decision maker requires a human counterparty")
        if self.decision_maker is True and self.interlocutor_role != "decision_maker":
            raise ValueError("A decision maker requires the explicit role")
        return self

    def validate_outcome(self, outcome):
        if outcome in {"no_answer", "voicemail", "wrong_number"} and self.answer_kind not in {outcome, "unknown"}:
            raise ValueError("Contradictory call outcome")
