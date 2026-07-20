from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    type: str
    occurred_at: datetime


class LeadSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    account_id: UUID | None
    company: str
    contact_name: str | None
    email: str | None
    phone: str | None
    stage: str
    source_stage: str | None
    priority: str | None
    proposal_count: int = Field(ge=0)
    next_action: str | None
    next_action_due_at: datetime | None
    updated_at: datetime


class LeadPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[LeadSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class AccountSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    display_name: str
    lifecycle_stage: str
    highest_stage_rank: int
    sector: str | None
    contact_count: int = Field(ge=0)
    email_count: int = Field(ge=0)
    sent_email_count: int = Field(ge=0)
    received_email_count: int = Field(ge=0)
    meeting_count: int = Field(ge=0)
    booked_meeting_count: int = Field(ge=0)
    held_meeting_count: int = Field(ge=0)
    cancelled_meeting_count: int = Field(ge=0)
    no_show_meeting_count: int = Field(ge=0)
    proposal_count: int = Field(ge=0)
    probability: float | None = Field(default=None, ge=0, le=1)
    next_action: str | None


class AccountDetail(AccountSummary):
    evidence_refs: tuple[EvidenceReference, ...]


class AccountPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[AccountSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
