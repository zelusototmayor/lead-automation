from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dashboard.app.db import create_database_engine, create_session_factory
from dashboard.app.feature_flags import require_postgres_command_writer
from dashboard.app.schemas.lead_commands import (
    EditLeadCommandBody,
    LeadOperationResult,
    LogCallCommandBody,
    LogEmailCommandBody,
    ScheduleNextActionCommandBody,
)
from dashboard.app.security import (
    CRMPrincipal,
    require_call_log_command_access,
    require_email_log_command_access,
    require_lead_edit_command_access,
    require_next_action_command_access,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    HumanCommandPrincipal,
)
from src.crm.services.lead_operation_service import (
    EditLeadCommand,
    LeadOperationService,
    LogCallCommand,
    LogEmailCommand,
    ScheduleNextActionCommand,
)

router = APIRouter()


@dataclass(frozen=True)
class LeadOperationContext:
    principal: CRMPrincipal
    session_factory: sessionmaker[Session]


@lru_cache(maxsize=1)
def _lead_operation_engine() -> Engine:
    return create_database_engine()


def _context(principal: CRMPrincipal) -> LeadOperationContext:
    require_postgres_command_writer()
    try:
        factory = create_session_factory(_lead_operation_engine())
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Writer unavailable") from None
    return LeadOperationContext(principal=principal, session_factory=factory)


def get_lead_edit_context(
    principal: Annotated[CRMPrincipal, Depends(require_lead_edit_command_access)],
) -> LeadOperationContext:
    return _context(principal)


def get_call_log_context(
    principal: Annotated[CRMPrincipal, Depends(require_call_log_command_access)],
) -> LeadOperationContext:
    return _context(principal)


def get_email_log_context(
    principal: Annotated[CRMPrincipal, Depends(require_email_log_command_access)],
) -> LeadOperationContext:
    return _context(principal)


def get_next_action_context(
    principal: Annotated[CRMPrincipal, Depends(require_next_action_command_access)],
) -> LeadOperationContext:
    return _context(principal)


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


def _principal(principal: CRMPrincipal) -> HumanCommandPrincipal:
    if principal.actor_id is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    return HumanCommandPrincipal(
        actor_id=principal.actor_id,
        workspace_id=principal.workspace_id,
        permissions=principal.permissions,
    )


def _result(result) -> LeadOperationResult:
    return LeadOperationResult(
        command_id=result.command_id,
        lead_id=result.aggregate_id,
        version=result.version,
        replayed=result.replayed,
        task_id=result.task_id,
        occurred_at=result.occurred_at,
    )


@router.post(
    "/api/v1/commands/leads/{lead_id}/edit",
    response_model=LeadOperationResult,
    response_model_exclude_none=True,
)
def edit_lead(
    lead_id: UUID,
    body: EditLeadCommandBody,
    context: Annotated[LeadOperationContext, Depends(get_lead_edit_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> LeadOperationResult:
    _command_id(idempotency_key, body.command_id)
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = LeadOperationService(uow).edit(
                _principal(context.principal),
                EditLeadCommand(
                    command_id=body.command_id,
                    workspace_id=context.principal.workspace_id,
                    lead_id=lead_id,
                    expected_version=body.expected_version,
                    priority=body.priority,
                    company_name=body.company_name,
                    contact_name=body.contact_name,
                    contact_email=body.contact_email,
                    contact_phone=body.contact_phone,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except (CommandConflictError, IntegrityError):
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return _result(result)


@router.post(
    "/api/v1/commands/leads/{lead_id}/log-call",
    response_model=LeadOperationResult,
    response_model_exclude_none=True,
)
def log_call(
    lead_id: UUID,
    body: LogCallCommandBody,
    context: Annotated[LeadOperationContext, Depends(get_call_log_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> LeadOperationResult:
    _command_id(idempotency_key, body.command_id)
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = LeadOperationService(uow).log_call(
                _principal(context.principal),
                LogCallCommand(
                    command_id=body.command_id,
                    workspace_id=context.principal.workspace_id,
                    lead_id=lead_id,
                    expected_version=body.expected_version,
                    outcome_code=body.outcome_code,
                    summary=body.summary,
                    occurred_at=body.occurred_at,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except (CommandConflictError, IntegrityError):
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return _result(result)


@router.post(
    "/api/v1/commands/leads/{lead_id}/log-email",
    response_model=LeadOperationResult,
    response_model_exclude_none=True,
)
def log_email(
    lead_id: UUID,
    body: LogEmailCommandBody,
    context: Annotated[LeadOperationContext, Depends(get_email_log_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> LeadOperationResult:
    _command_id(idempotency_key, body.command_id)
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = LeadOperationService(uow).log_email(
                _principal(context.principal),
                LogEmailCommand(
                    command_id=body.command_id,
                    workspace_id=context.principal.workspace_id,
                    lead_id=lead_id,
                    expected_version=body.expected_version,
                    direction=body.direction,
                    summary=body.summary,
                    occurred_at=body.occurred_at,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except (CommandConflictError, IntegrityError):
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return _result(result)


@router.post(
    "/api/v1/commands/leads/{lead_id}/schedule-next-action",
    response_model=LeadOperationResult,
    response_model_exclude_none=True,
)
def schedule_next_action(
    lead_id: UUID,
    body: ScheduleNextActionCommandBody,
    context: Annotated[LeadOperationContext, Depends(get_next_action_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> LeadOperationResult:
    _command_id(idempotency_key, body.command_id)
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = LeadOperationService(uow).schedule_next_action(
                _principal(context.principal),
                ScheduleNextActionCommand(
                    command_id=body.command_id,
                    workspace_id=context.principal.workspace_id,
                    lead_id=lead_id,
                    expected_version=body.expected_version,
                    task_type=body.task_type,
                    title=body.title,
                    due_at=body.due_at,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except (CommandConflictError, IntegrityError):
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return _result(result)
