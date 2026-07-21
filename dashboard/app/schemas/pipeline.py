from __future__ import annotations

from datetime import date, datetime
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
PipelineQueueUnit = Literal["task", "lead"]


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
    queue_units: dict[PipelineQueue, PipelineQueueUnit]
    generated_at: datetime


class AnalyticsPeriod(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_date: date
    end_date: date
    days: int = Field(ge=1, le=120)


class AnalyticsDay(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    activity_types: dict[str, int]
    outcomes: dict[str, int]
    distinct_touched_leads: int = Field(ge=0)


class AnalyticsCountBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    by_status: dict[str, int]
    total: int = Field(ge=0)


class AnalyticsTaskBreakdown(AnalyticsCountBreakdown):
    open_by_type: dict[str, int]


class AnalyticsQueueBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    counts: dict[str, int]
    unit: Literal["task"] = "task"


class PipelineAnalytics(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: AnalyticsPeriod
    daily: tuple[AnalyticsDay, ...]
    stages: AnalyticsCountBreakdown
    proposals: AnalyticsCountBreakdown
    tasks: AnalyticsTaskBreakdown
    queues: AnalyticsQueueBreakdown
    time_in_stage: Literal["not_available"] = "not_available"
    generated_at: datetime


class LeadDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    account_id: UUID | None
    company: str
    contact_name: str | None
    email: str | None
    phone: str | None
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
