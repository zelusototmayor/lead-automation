from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


PipelineQueue = Literal[
    "calls_overdue",
    "calls_today",
    "calls_future",
    "emails_overdue",
    "emails_today",
    "emails_future",
    "proposal_followups_overdue",
    "proposal_followups_today",
    "touched_today",
    "untouched",
    "all",
]
PipelinePriority = Literal["low", "medium", "high"]


class PipelineTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    type: str
    title: str
    due_at: datetime
    version: int = Field(ge=1)


class PipelineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    lead_id: UUID
    account_id: UUID | None
    company: str
    contact_name: str | None
    email: str | None
    phone: str | None
    city: str | None
    stage: str
    priority: str | None
    lead_version: int = Field(ge=1)
    task: PipelineTask | None


class PipelinePage(BaseModel):
    model_config = ConfigDict(frozen=True)

    queue: PipelineQueue
    items: tuple[PipelineItem, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class PipelineSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    queues: dict[PipelineQueue, int]
    generated_at: datetime


class LeadDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    account_id: UUID | None
    company: str
    contact_name: str | None
    email: str | None
    phone: str | None
    city: str | None
    stage: str
    priority: str | None
    version: int = Field(ge=1)


class TimelineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    type: str
    title: str
    summary: str | None
    outcome_code: str | None
    direction: str | None
    occurred_at: datetime


class TimelinePage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[TimelineItem, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class LeadTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    type: str
    title: str
    due_at: datetime
    status: str
    version: int = Field(ge=1)


class LeadTaskPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[LeadTask, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
