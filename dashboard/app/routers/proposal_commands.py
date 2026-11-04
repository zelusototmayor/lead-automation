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
from dashboard.app.schemas.proposal_commands import (
    ProposalOperationResult,
    UpdateProposalPipelineBody,
)
from dashboard.app.security import (
    CRMPrincipal,
    require_proposal_write_command_access,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    HumanCommandPrincipal,
)
from src.crm.services.proposal_operation_service import (
    ProposalOperationService,
    UpdateProposalPipelineCommand,
)

router = APIRouter()


@dataclass(frozen=True)
class ProposalOperationContext:
    principal: CRMPrincipal
    session_factory: sessionmaker[Session]


@lru_cache(maxsize=1)
def _proposal_operation_engine() -> Engine:
    return create_database_engine()


def get_proposal_operation_context(
    principal: Annotated[CRMPrincipal, Depends(require_proposal_write_command_access)],
) -> ProposalOperationContext:
    require_postgres_command_writer()
    try:
        factory = create_session_factory(_proposal_operation_engine())
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Writer unavailable") from None
    return ProposalOperationContext(principal=principal, session_factory=factory)


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


@router.post(
    "/api/v1/commands/proposals/{proposal_id}/update-pipeline",
    response_model=ProposalOperationResult,
)
def update_proposal_pipeline(
    proposal_id: UUID,
    body: UpdateProposalPipelineBody,
    context: Annotated[
        ProposalOperationContext, Depends(get_proposal_operation_context)
    ],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ProposalOperationResult:
    _command_id(idempotency_key, body.command_id)
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = ProposalOperationService(uow).update_pipeline(
                _principal(context.principal),
                UpdateProposalPipelineCommand(
                    command_id=body.command_id,
                    workspace_id=context.principal.workspace_id,
                    proposal_id=proposal_id,
                    expected_version=body.expected_version,
                    status=body.status,
                    probability=body.probability,
                    forecast_category=body.forecast_category,
                    next_action=body.next_action,
                    next_action_due_at=body.next_action_due_at,
                    lost_reason=body.lost_reason,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except (CommandConflictError, IntegrityError):
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return ProposalOperationResult(
        command_id=result.command_id,
        proposal_id=result.aggregate_id,
        version=result.version,
        replayed=result.replayed,
    )
