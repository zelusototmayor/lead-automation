from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProposalStatus = Literal[
    "draft",
    "promised",
    "sent",
    "viewed",
    "negotiation",
    "won",
    "lost",
    "withdrawn",
    "expired",
]


class UpdateProposalPipelineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    expected_version: int = Field(ge=1)
    status: ProposalStatus
    probability: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    forecast_category: str | None = Field(default=None, min_length=1, max_length=64)
    next_action: str | None = Field(default=None, min_length=1, max_length=2048)
    next_action_due_at: datetime | None = None
    lost_reason: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_pipeline_state(self):
        for value in (self.forecast_category, self.next_action, self.lost_reason):
            if value is not None and value != value.strip():
                raise ValueError("invalid pipeline state")
        if self.next_action_due_at is not None:
            if (
                self.next_action is None
                or self.next_action_due_at.tzinfo is None
                or self.next_action_due_at.utcoffset() is None
            ):
                raise ValueError("invalid pipeline state")
        if (self.status == "lost") != (self.lost_reason is not None):
            raise ValueError("invalid pipeline state")
        return self


class ProposalOperationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID
    proposal_id: UUID
    version: int = Field(ge=1)
    replayed: bool
