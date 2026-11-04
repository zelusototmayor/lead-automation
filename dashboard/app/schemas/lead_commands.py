from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from typing import Literal

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
    capacity_minutes: StrictInt = Field(default=120, ge=0, le=480)
    target_contacts: StrictInt = Field(default=40, ge=1, le=100)
    reconcile: bool = False
    replenish: bool = False
    context_sha256: StrictStr | None = Field(default=None, pattern='^[0-9a-f]{64}$')
    restore_command_id: UUID | None = None

    @model_validator(mode='after')
    def reconciliation_contract(self):
        if self.replenish and (not self.reconcile or self.restore_command_id is not None):
            raise ValueError('replenishment requires reconciliation without restore')
        if self.reconcile:
            if self.expected_version < 1 or self.context_sha256 is None:
                raise ValueError('reconciliation requires current version and source CAS')
        elif self.context_sha256 is not None or self.restore_command_id is not None:
            raise ValueError('reconciliation fields on preparation')
        return self


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
    outcome_code: StrictStr | None = Field(default=None, min_length=1, max_length=64)
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
    note_kind: Literal['lead_justification'] | None = None
    provenance_ref: StrictStr | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def valid_automated_note(self):
        if (self.note_kind is None) != (self.provenance_ref is None):
            raise ValueError('Automated note requires kind and provenance')
        if self.note_kind and not self.summary.startswith('Justificação de lead: '):
            raise ValueError('Invalid lead justification')
        return self


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
