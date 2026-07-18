from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from dashboard.app.db import create_database_engine
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.schemas.accounts import (
    AccountDetail,
    AccountPage,
    AccountSummary,
    EvidenceReference,
)
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    EmailMessage,
    Meeting,
    Proposal,
    Task,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountRequestContext:
    principal: CRMPrincipal
    session: Session


@lru_cache(maxsize=1)
def _account_engine() -> Engine:
    return create_database_engine()


def _unconfigured_account_shadow_comparison(_principal: CRMPrincipal) -> None:
    raise RuntimeError("account shadow comparison is not configured")


_account_shadow_comparison = _unconfigured_account_shadow_comparison


def get_account_request_context(
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
):
    """Open a read session only after identity and cutover gates pass."""

    try:
        flags = get_feature_flags()
    except ValueError:
        flags = None
    if flags is None or not flags.database_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Accounts unavailable",
        )
    if flags.accounts_read_model == "shadow":
        try:
            _account_shadow_comparison(principal)
        except Exception:
            logger.warning("account shadow comparison failed")
        else:
            logger.info("account shadow comparison completed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Accounts unavailable",
        )
    if flags.accounts_read_model != "postgres":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Accounts unavailable",
        )
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
        select(func.count(EmailMessage.id))
        .where(
            EmailMessage.workspace_id == workspace_id,
            EmailMessage.account_id == Account.id,
        )
        .correlate(Account)
        .scalar_subquery()
    )
    sent_email_count = (
        select(func.count(EmailMessage.id))
        .where(
            EmailMessage.workspace_id == workspace_id,
            EmailMessage.account_id == Account.id,
            EmailMessage.direction == "outbound",
        )
        .correlate(Account)
        .scalar_subquery()
    )
    received_email_count = (
        select(func.count(EmailMessage.id))
        .where(
            EmailMessage.workspace_id == workspace_id,
            EmailMessage.account_id == Account.id,
            EmailMessage.direction == "inbound",
        )
        .correlate(Account)
        .scalar_subquery()
    )
    meeting_count = (
        select(func.count(Meeting.id))
        .where(
            Meeting.workspace_id == workspace_id,
            Meeting.account_id == Account.id,
        )
        .correlate(Account)
        .scalar_subquery()
    )

    def meeting_status_count(status_value: str):
        return (
            select(func.count(Meeting.id))
            .where(
                Meeting.workspace_id == workspace_id,
                Meeting.account_id == Account.id,
                Meeting.status == status_value,
            )
            .correlate(Account)
            .scalar_subquery()
        )

    booked_meeting_count = meeting_status_count("booked")
    held_meeting_count = meeting_status_count("held")
    cancelled_meeting_count = meeting_status_count("cancelled")
    no_show_meeting_count = meeting_status_count("no_show")
    proposal_count = (
        select(func.count(Proposal.id))
        .where(
            Proposal.workspace_id == workspace_id,
            Proposal.account_id == Account.id,
        )
        .correlate(Account)
        .scalar_subquery()
    )
    next_action = (
        select(Task.title)
        .where(
            Task.workspace_id == workspace_id,
            Task.account_id == Account.id,
            Task.status == "open",
        )
        .order_by(Task.due_at.asc().nulls_last(), Task.created_at.asc(), Task.id.asc())
        .limit(1)
        .correlate(Account)
        .scalar_subquery()
    )
    return (
        contact_count,
        email_count,
        sent_email_count,
        received_email_count,
        meeting_count,
        booked_meeting_count,
        held_meeting_count,
        cancelled_meeting_count,
        no_show_meeting_count,
        proposal_count,
        next_action,
    )


def _summary_statement(workspace_id: UUID):
    (
        contact_count,
        email_count,
        sent_email_count,
        received_email_count,
        meeting_count,
        booked_meeting_count,
        held_meeting_count,
        cancelled_meeting_count,
        no_show_meeting_count,
        proposal_count,
        next_action,
    ) = _summary_columns(workspace_id)
    return select(
        Account.id,
        Account.display_name,
        Account.lifecycle_stage,
        Account.highest_stage_rank,
        Account.sector,
        contact_count.label("contact_count"),
        email_count.label("email_count"),
        sent_email_count.label("sent_email_count"),
        received_email_count.label("received_email_count"),
        meeting_count.label("meeting_count"),
        booked_meeting_count.label("booked_meeting_count"),
        held_meeting_count.label("held_meeting_count"),
        cancelled_meeting_count.label("cancelled_meeting_count"),
        no_show_meeting_count.label("no_show_meeting_count"),
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
        sent_email_count=row.sent_email_count,
        received_email_count=row.received_email_count,
        meeting_count=row.meeting_count,
        booked_meeting_count=row.booked_meeting_count,
        held_meeting_count=row.held_meeting_count,
        cancelled_meeting_count=row.cancelled_meeting_count,
        no_show_meeting_count=row.no_show_meeting_count,
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
