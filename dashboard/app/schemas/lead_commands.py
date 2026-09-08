from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from src.crm.domain.call_contract import CallDetails, PhoneHistory, CallIntent


class LeadOperationBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    expected_version: StrictInt = Field(ge=1)


class PrepareCallDayBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    command_id: UUID
    work_date: date
    expected_version: StrictInt = Field(ge=0)
    capacity_minutes: StrictInt = Field(ge=0, le=480)


class RecordPhoneHistoryCommandBody(LeadOperationBase):
    phone_history: PhoneHistory


class EditLeadCommandBody(LeadOperationBase):
    priority: StrictStr = Field(min_length=1, max_length=64)
    company_name: StrictStr = Field(min_length=1, max_length=512)
    contact_name: StrictStr | None = Field(default=None, max_length=512)
    contact_email: StrictStr | None = Field(
        default=None, max_length=320, pattern=r"^(?:[^\s@]+@[^\s@]+)?$"
    )
    contact_phone: StrictStr | None = Field(default=None, max_length=64)


class CallNextAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    call_intent: CallIntent | None = None
    task_type: StrictStr = Field(pattern="^call$")
    title: StrictStr = Field(min_length=1, max_length=512)
    due_at: AwareDatetime


class CompletedCallTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    expected_version: StrictInt = Field(ge=1)


class LogCallCommandBody(LeadOperationBase):
    call_details: CallDetails | None = None
    completed_task: CompletedCallTask | None = None
    next_action: CallNextAction | None = None
    outcome_code: StrictStr = Field(min_length=1, max_length=64)
    summary: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    occurred_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_call_outcome(self):
        if self.call_details is not None:
            self.call_details.validate_outcome(self.outcome_code)
        return self


class LogEmailCommandBody(LeadOperationBase):
    direction: StrictStr = Field(pattern="^(inbound|outbound)$")
    summary: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    occurred_at: AwareDatetime | None = None


class AddNoteCommandBody(LeadOperationBase):
    summary: StrictStr = Field(min_length=1, max_length=2000)


class ScheduleNextActionCommandBody(LeadOperationBase):
    call_intent: CallIntent | None = None
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
    callback_sync_status: str | None = None
    occurred_at: datetime | None = None
    activity_id: UUID | None = None
    call_details: dict | None = None
    phone_history: dict | None = None
