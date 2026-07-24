from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt


class CompleteTaskCommandBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    expected_version: StrictInt = Field(ge=1)


class RescheduleTaskCommandBody(CompleteTaskCommandBody):
    due_at: AwareDatetime


class CancelTaskCommandBody(CompleteTaskCommandBody):
    pass


class TaskCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID
    task_id: UUID
    version: int = Field(ge=1)
    replayed: bool
