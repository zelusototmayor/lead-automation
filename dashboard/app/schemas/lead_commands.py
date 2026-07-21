from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr


class LeadOperationBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    expected_version: StrictInt = Field(ge=1)


class EditLeadCommandBody(LeadOperationBase):
    priority: StrictStr = Field(min_length=1, max_length=64)
    company_name: StrictStr = Field(min_length=1, max_length=512)
    contact_name: StrictStr = Field(min_length=1, max_length=512)
    contact_email: StrictStr = Field(
        min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+$"
    )
    contact_phone: StrictStr = Field(min_length=1, max_length=64)


class LogCallCommandBody(LeadOperationBase):
    outcome_code: StrictStr = Field(min_length=1, max_length=64)
    summary: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    occurred_at: AwareDatetime | None = None


class LogEmailCommandBody(LeadOperationBase):
    direction: StrictStr = Field(pattern="^(inbound|outbound)$")
    summary: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    occurred_at: AwareDatetime | None = None


class ScheduleNextActionCommandBody(LeadOperationBase):
    task_type: StrictStr = Field(pattern="^(call|email|follow_up)$")
    title: StrictStr = Field(min_length=1, max_length=512)
    due_at: AwareDatetime


class LeadOperationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID
    lead_id: UUID
    version: int = Field(ge=1)
    replayed: bool
    task_id: UUID | None = None
    occurred_at: datetime | None = None
