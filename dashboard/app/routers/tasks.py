from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from dashboard.app.db import create_database_engine, create_session_factory
from dashboard.app.feature_flags import require_postgres_command_writer
from dashboard.app.schemas.tasks import (
    CancelTaskCommandBody,
    CompleteTaskCommandBody,
    RescheduleTaskCommandBody,
    TaskCommandResult,
)
from dashboard.app.security import CRMPrincipal, require_task_command_access
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    HumanCommandPrincipal,
)
from src.crm.services.task_command_service import (
    CancelTaskCommand,
    CompleteTaskCommand,
    RescheduleTaskCommand,
    TaskCommandService,
)

router = APIRouter()


@dataclass(frozen=True)
class TaskCommandContext:
    principal: CRMPrincipal
    session_factory: sessionmaker[Session]


@lru_cache(maxsize=1)
def _task_engine() -> Engine:
    return create_database_engine()


def get_task_command_context(
    principal: Annotated[CRMPrincipal, Depends(require_task_command_access)],
) -> TaskCommandContext:
    """Open command resources only after exact auth, CSRF, and Origin checks."""

    require_postgres_command_writer()
    try:
        factory = create_session_factory(_task_engine())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Writer unavailable",
        ) from None
    return TaskCommandContext(principal=principal, session_factory=factory)


def _command_id(idempotency_key: str | None, body_command_id: UUID) -> UUID:
    if idempotency_key is None:
        raise HTTPException(status_code=422, detail="Invalid command")
    try:
        header_command_id = UUID(idempotency_key)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid command") from None
    if header_command_id != body_command_id:
        raise HTTPException(status_code=409, detail="Command conflict")
    return header_command_id


@router.post(
    "/api/v1/commands/tasks/{task_id}/complete",
    response_model=TaskCommandResult,
)
def complete_task(
    task_id: UUID,
    body: CompleteTaskCommandBody,
    context: Annotated[TaskCommandContext, Depends(get_task_command_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskCommandResult:
    _command_id(idempotency_key, body.command_id)
    principal = context.principal
    assert principal.actor_id is not None
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = TaskCommandService(uow).complete(
                HumanCommandPrincipal(
                    actor_id=principal.actor_id,
                    workspace_id=principal.workspace_id,
                    permissions=principal.permissions,
                ),
                CompleteTaskCommand(
                    command_id=body.command_id,
                    workspace_id=principal.workspace_id,
                    task_id=task_id,
                    expected_version=body.expected_version,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except CommandConflictError:
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return TaskCommandResult(
        command_id=result.command_id,
        task_id=result.aggregate_id,
        version=result.version,
        replayed=result.replayed,
    )


@router.post(
    "/api/v1/commands/tasks/{task_id}/reschedule",
    response_model=TaskCommandResult,
)
def reschedule_task(
    task_id: UUID,
    body: RescheduleTaskCommandBody,
    context: Annotated[TaskCommandContext, Depends(get_task_command_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskCommandResult:
    _command_id(idempotency_key, body.command_id)
    principal = context.principal
    assert principal.actor_id is not None
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = TaskCommandService(uow).reschedule(
                HumanCommandPrincipal(
                    actor_id=principal.actor_id,
                    workspace_id=principal.workspace_id,
                    permissions=principal.permissions,
                ),
                RescheduleTaskCommand(
                    command_id=body.command_id,
                    workspace_id=principal.workspace_id,
                    task_id=task_id,
                    expected_version=body.expected_version,
                    due_at=body.due_at,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except CommandConflictError:
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return TaskCommandResult(
        command_id=result.command_id,
        task_id=result.aggregate_id,
        version=result.version,
        replayed=result.replayed,
    )


@router.post(
    "/api/v1/commands/tasks/{task_id}/cancel",
    response_model=TaskCommandResult,
)
def cancel_task(
    task_id: UUID,
    body: CancelTaskCommandBody,
    context: Annotated[TaskCommandContext, Depends(get_task_command_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskCommandResult:
    _command_id(idempotency_key, body.command_id)
    principal = context.principal
    assert principal.actor_id is not None
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = TaskCommandService(uow).cancel(
                HumanCommandPrincipal(
                    actor_id=principal.actor_id,
                    workspace_id=principal.workspace_id,
                    permissions=principal.permissions,
                ),
                CancelTaskCommand(
                    command_id=body.command_id,
                    workspace_id=principal.workspace_id,
                    task_id=task_id,
                    expected_version=body.expected_version,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except CommandConflictError:
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return TaskCommandResult(
        command_id=result.command_id,
        task_id=result.aggregate_id,
        version=result.version,
        replayed=result.replayed,
    )
