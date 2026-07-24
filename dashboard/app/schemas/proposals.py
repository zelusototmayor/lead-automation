from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProposalSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    version: int = Field(ge=1)
    account_id: UUID
    account_name: str
    commercial_vertical: str | None
    title: str
    status: str
    currency: str
    probability: Decimal | None
    probability_source: str | None
    forecast_category: str | None
    next_action: str | None
    next_action_due_at: datetime | None
    owner_id: UUID | None
    value_state: str
    one_off_amount: Decimal | None
    mrr_amount: Decimal | None
    arr_amount: Decimal | None
    sent_at: datetime | None
    sent_verification_state: str | None
    age_days: int | None = Field(ge=0)
    followup_count: int = Field(ge=0)
    last_interaction_at: datetime | None


class ProposalPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[ProposalSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class ProposalItemDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    description: str
    quantity: Decimal | None
    unit_price: Decimal | None
    billing_period: str | None
    option_group: str | None
    is_selected: bool
    amount: Decimal | None
    currency: str


class ProposalVersionDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    version_number: int = Field(ge=1)
    status: str
    created_at: datetime
    sent_at: datetime | None
    valid_until: date | None
    one_off_amount: Decimal | None
    mrr_amount: Decimal | None
    arr_amount: Decimal | None
    tax_inclusion: str
    source_document_evidence_id: UUID | None
    extraction_confidence: Decimal | None
    confirmed_by: None = None
    confirmed_at: datetime | None
    items: tuple[ProposalItemDetail, ...]


class ProposalFollowupReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    activity_id: UUID
    sequence_number: int = Field(ge=1)
    occurred_at: datetime
    channel: str


class ProposalDetail(ProposalSummary):
    sent_evidence_id: UUID | None
    lost_reason: str | None
    won_at: datetime | None
    lost_at: datetime | None
    versions: tuple[ProposalVersionDetail, ...]
    followups: tuple[ProposalFollowupReference, ...]


class DimensionTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    one_off: Decimal = Decimal("0.00")
    mrr: Decimal = Decimal("0.00")
    arr: Decimal = Decimal("0.00")


class ValueStateCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    missing: int = Field(ge=0)
    candidate: int = Field(ge=0)
    confirmed: int = Field(ge=0)
    rejected: int = Field(ge=0)


class ProposalPortfolio(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_count: int = Field(ge=0)
    value_counts: ValueStateCounts
    status_counts: dict[str, int]
    totals: dict[str, DimensionTotals]
    open_pipeline: dict[str, DimensionTotals]
    weighted_pipeline: dict[str, DimensionTotals]
    won_totals: dict[str, DimensionTotals]
    lost_totals: dict[str, DimensionTotals]
