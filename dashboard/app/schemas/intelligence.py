from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RecommendationSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    account_id: UUID
    account_name: str
    proposal_id: UUID | None
    rule_code: str
    priority: str
    evidence: tuple[str, ...]
    state: str
    observed_at: datetime


class RecommendationPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[RecommendationSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
