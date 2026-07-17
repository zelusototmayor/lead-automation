from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, literal, select
from sqlalchemy.orm import Session

from dashboard.app.db import create_database_engine
from dashboard.app.feature_flags import require_accounts_postgres_reads
from dashboard.app.schemas.accounts import (
    AccountDetail,
    AccountPage,
    AccountSummary,
    EvidenceReference,
)
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import Account, Activity, Contact

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))


@dataclass(frozen=True)
class AccountRequestContext:
    principal: CRMPrincipal
    session: Session


@lru_cache(maxsize=1)
def _account_engine() -> Engine:
    return create_database_engine()


def get_account_request_context(
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
):
    """Open a read session only after identity and cutover gates pass."""

    require_accounts_postgres_reads()
    try:
        engine = _account_engine()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Accounts unavailable",
        ) from None
    with Session(engine) as session:
        yield AccountRequestContext(principal=principal, session=session)


def _summary_columns(workspace_id: UUID):
    contact_count = (
        select(func.count(Contact.id))
        .where(
            Contact.workspace_id == workspace_id,
            Contact.account_id == Account.id,
        )
        .correlate(Account)
        .scalar_subquery()
    )
    email_count = (
        select(func.count(Activity.id))
        .where(
            Activity.workspace_id == workspace_id,
            Activity.account_id == Account.id,
            Activity.activity_type.in_(("email_sent", "email_received")),
        )
        .correlate(Account)
        .scalar_subquery()
    )
    meeting_count = (
        select(func.count(Activity.id))
        .where(
            Activity.workspace_id == workspace_id,
            Activity.account_id == Account.id,
            Activity.activity_type == "meeting",
        )
        .correlate(Account)
        .scalar_subquery()
    )
    proposal_count = (
        select(func.count(Activity.id))
        .where(
            Activity.workspace_id == workspace_id,
            Activity.account_id == Account.id,
            Activity.activity_type == "proposal",
        )
        .correlate(Account)
        .scalar_subquery()
    )
    next_action = literal(None)
    return contact_count, email_count, meeting_count, proposal_count, next_action


def _summary_statement(workspace_id: UUID):
    contact_count, email_count, meeting_count, proposal_count, next_action = (
        _summary_columns(workspace_id)
    )
    return select(
        Account.id,
        Account.display_name,
        Account.lifecycle_stage,
        Account.highest_stage_rank,
        Account.sector,
        contact_count.label("contact_count"),
        email_count.label("email_count"),
        meeting_count.label("meeting_count"),
        proposal_count.label("proposal_count"),
        next_action.label("next_action"),
    ).where(
        Account.workspace_id == workspace_id,
        Account.merged_into_account_id.is_(None),
    )


def _to_summary(row) -> AccountSummary:
    return AccountSummary(
        id=row.id,
        display_name=row.display_name,
        lifecycle_stage=row.lifecycle_stage,
        highest_stage_rank=row.highest_stage_rank,
        sector=row.sector,
        contact_count=row.contact_count,
        email_count=row.email_count,
        meeting_count=row.meeting_count,
        proposal_count=row.proposal_count,
        probability=None,
        next_action=row.next_action,
    )


def _account_or_404(context: AccountRequestContext, account_id: UUID):
    row = context.session.execute(
        _summary_statement(context.principal.workspace_id).where(
            Account.id == account_id
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return row


@router.get("/api/v1/accounts", response_model=AccountPage)
def list_accounts(
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AccountPage:
    workspace_id = context.principal.workspace_id
    total = context.session.scalar(
        select(func.count(Account.id)).where(
            Account.workspace_id == workspace_id,
            Account.merged_into_account_id.is_(None),
        )
    )
    rows = context.session.execute(
        _summary_statement(workspace_id)
        .order_by(Account.display_name.asc(), Account.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    return AccountPage(
        items=tuple(_to_summary(row) for row in rows),
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/accounts/{account_id}", response_model=AccountDetail)
def account_detail(
    account_id: UUID,
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
) -> AccountDetail:
    summary = _to_summary(_account_or_404(context, account_id))
    evidence = context.session.execute(
        select(Activity.id, Activity.activity_type, Activity.occurred_at)
        .where(
            Activity.workspace_id == context.principal.workspace_id,
            Activity.account_id == account_id,
        )
        .order_by(Activity.occurred_at.desc(), Activity.id.desc())
        .limit(50)
    ).all()
    return AccountDetail(
        **summary.model_dump(),
        evidence_refs=tuple(
            EvidenceReference(
                id=row.id, type=row.activity_type, occurred_at=row.occurred_at
            )
            for row in evidence
        ),
    )


@router.get("/contas", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
):
    return templates.TemplateResponse(
        request,
        "accounts/index.html",
        {"request": request, "subject": context.principal.subject},
    )


@router.get("/contas/{account_id}", response_class=HTMLResponse)
def account_page(
    request: Request,
    account_id: UUID,
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
):
    _account_or_404(context, account_id)
    return templates.TemplateResponse(
        request,
        "accounts/detail.html",
        {
            "request": request,
            "account_id": str(account_id),
            "subject": context.principal.subject,
        },
    )
