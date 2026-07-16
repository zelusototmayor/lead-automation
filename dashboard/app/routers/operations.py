"""Admin-only, workspace-scoped CRM operational health reads."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import Engine, and_, exists, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from dashboard.app.db import create_database_engine
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Account,
    IngestEvent,
    Lead,
    OutboxEvent,
    Proposal,
    SyncCheckpoint,
)

router = APIRouter()


@dataclass(frozen=True)
class OperationsRequestContext:
    principal: CRMPrincipal
    session: Session


@lru_cache(maxsize=1)
def _operations_engine() -> Engine:
    return create_database_engine()


def get_operations_request_context(
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
):
    if principal.is_admin is not True:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    try:
        engine = _operations_engine()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operations unavailable",
        ) from None
    with Session(engine) as session:
        yield OperationsRequestContext(principal, session)


@router.get("/api/v1/operations/metrics")
def operations_metrics(
    context: Annotated[
        OperationsRequestContext, Depends(get_operations_request_context)
    ],
):
    workspace_id = context.principal.workspace_id
    session = context.session

    def count(statement) -> int:
        return int(session.scalar(statement) or 0)

    def age_seconds(
        model,
        timestamp_column,
        status_column=None,
        statuses: tuple[str, ...] | None = None,
    ):
        conditions = [model.workspace_id == workspace_id, timestamp_column.is_not(None)]
        if status_column is not None and statuses is not None:
            conditions.append(status_column.in_(statuses))
        value = session.scalar(
            select(
                func.extract("epoch", func.now() - func.min(timestamp_column))
            ).where(*conditions)
        )
        return None if value is None else max(0, int(value))

    try:
        session.scalar(select(literal(1)))
        observed_at = session.scalar(select(func.now()))
        account_rank_violation = exists(
            select(Account.id).where(
                Account.workspace_id == Lead.workspace_id,
                Account.id == Lead.account_id,
                Lead.highest_stage_rank > Account.highest_stage_rank,
            )
        )
        metrics = {
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "database": {"status": "ok"},
            "event_lag_seconds": age_seconds(
                IngestEvent,
                IngestEvent.received_at,
                IngestEvent.processing_status,
                ("received", "processing", "review", "failed"),
            ),
            "checkpoint_age_seconds": age_seconds(
                SyncCheckpoint, SyncCheckpoint.last_success_at
            ),
            "dead_letter_count": count(
                select(func.count(IngestEvent.id)).where(
                    IngestEvent.workspace_id == workspace_id,
                    IngestEvent.processing_status == "dead_letter",
                )
            ),
            "reconciliation_mismatch_count": count(
                select(func.count(IngestEvent.id)).where(
                    IngestEvent.workspace_id == workspace_id,
                    IngestEvent.processing_status == "review",
                )
            ),
            "missing_value_count": count(
                select(func.count(Proposal.id)).where(
                    Proposal.workspace_id == workspace_id,
                    Proposal.value_state == "missing",
                )
            ),
            "account_invariant_violation_count": count(
                select(func.count(Lead.id)).where(
                    Lead.workspace_id == workspace_id,
                    or_(
                        and_(
                            Lead.highest_stage_rank >= 40,
                            Lead.account_id.is_(None),
                        ),
                        account_rank_violation,
                    ),
                )
            ),
            "outbox_lag_seconds": age_seconds(
                OutboxEvent,
                OutboxEvent.created_at,
                OutboxEvent.status,
                ("pending", "publishing", "failed"),
            ),
        }
    except (SQLAlchemyError, AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operations unavailable",
        ) from None
    return metrics


@router.get("/operacoes", response_class=HTMLResponse)
def operations_page(
    context: Annotated[
        OperationsRequestContext, Depends(get_operations_request_context)
    ],
):
    del context
    return HTMLResponse(
        """<!doctype html><html><head><title>CRM Operations</title></head>
        <body><main><h1>CRM Operations</h1>
        <p>Admin-only workspace health. Unknown ages are shown as unavailable.</p>
        <div id="metrics" aria-live="polite">Loading operational metrics…</div>
        <script>fetch('/api/v1/operations/metrics', {credentials: 'same-origin'})
        .then(r => { if (!r.ok) throw new Error('unavailable'); return r.json(); })
        .then(m => { document.getElementById('metrics').textContent = JSON.stringify(m); })
        .catch(() => { document.getElementById('metrics').textContent =
          'Operational metrics unavailable'; });</script></main></body></html>"""
    )
