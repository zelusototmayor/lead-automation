from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select, true
from sqlalchemy.orm import Session, sessionmaker

from dashboard.app.config import get_settings
from dashboard.app.db import create_database_engine, create_session_factory
from dashboard.app.feature_flags import (
    get_feature_flags,
    require_postgres_command_writer,
)
from dashboard.app.schemas.accounts import (
    AccountDetail,
    AccountPage,
    AccountSummary,
    EvidenceReference,
    LeadPage,
    LeadStageCommand,
    LeadStageCommandResult,
    LeadSummary,
)
from dashboard.app.security import (
    CRMPrincipal,
    require_crm_command_access,
    require_crm_principal,
)
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    EmailMessage,
    Lead,
    Meeting,
    Proposal,
    Task,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    HumanCommandPrincipal,
    HumanCommandService,
    TransitionLeadCommand,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountRequestContext:
    principal: CRMPrincipal
    session: Session


@dataclass(frozen=True)
class LeadCommandContext:
    principal: CRMPrincipal
    session_factory: sessionmaker[Session]


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


def get_lead_command_context(
    principal: Annotated[CRMPrincipal, Depends(require_crm_command_access)],
) -> LeadCommandContext:
    """Resolve canonical command resources only after auth and write gates pass."""

    require_postgres_command_writer()
    try:
        factory = create_session_factory(_account_engine())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Writer unavailable",
        ) from None
    return LeadCommandContext(principal=principal, session_factory=factory)


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


def _lead_statement(workspace_id: UUID):
    proposal_count = (
        select(func.count(Proposal.id))
        .where(
            Proposal.workspace_id == workspace_id,
            Proposal.lead_id == Lead.id,
        )
        .correlate(Lead)
        .scalar_subquery()
    )
    next_task = (
        select(Task.title, Task.due_at)
        .where(
            Task.workspace_id == workspace_id,
            Task.account_id == Lead.account_id,
            Task.status.in_(("open", "in_progress")),
        )
        .order_by(Task.due_at.asc().nulls_last(), Task.id.asc())
        .limit(1)
        .lateral()
    )
    return (
        select(
            Lead.id,
            Lead.account_id,
            func.coalesce(Account.display_name, "Sem conta").label("company"),
            Contact.full_name.label("contact_name"),
            Contact.primary_email.label("email"),
            Contact.phone,
            Lead.stage,
            Lead.source_stage_raw.label("source_stage"),
            Lead.priority,
            proposal_count.label("proposal_count"),
            next_task.c.title.label("next_action"),
            next_task.c.due_at.label("next_action_due_at"),
            Lead.updated_at,
        )
        .outerjoin(
            Account,
            (Account.workspace_id == Lead.workspace_id)
            & (Account.id == Lead.account_id),
        )
        .outerjoin(
            Contact,
            (Contact.workspace_id == Lead.workspace_id)
            & (Contact.id == Lead.contact_id),
        )
        .outerjoin(next_task, true())
        .where(Lead.workspace_id == workspace_id)
    )


@router.get("/api/v1/leads", response_model=LeadPage)
def list_leads(
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeadPage:
    workspace_id = context.principal.workspace_id
    total = context.session.scalar(
        select(func.count(Lead.id)).where(Lead.workspace_id == workspace_id)
    )
    rows = context.session.execute(
        _lead_statement(workspace_id)
        .order_by(
            Lead.highest_stage_rank.desc(),
            Account.display_name.asc().nulls_last(),
            Lead.id.asc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()
    return LeadPage(
        items=tuple(
            LeadSummary(
                id=row.id,
                account_id=row.account_id,
                company=row.company,
                contact_name=row.contact_name,
                email=str(row.email) if row.email is not None else None,
                phone=row.phone,
                stage=row.stage,
                source_stage=row.source_stage,
                priority=row.priority,
                proposal_count=row.proposal_count,
                next_action=row.next_action,
                next_action_due_at=row.next_action_due_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ),
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/api/v1/commands/leads/{lead_id}/transition-stage",
    response_model=LeadStageCommandResult,
)
@router.put(
    "/api/v1/leads/{lead_id}/stage",
    response_model=LeadStageCommandResult,
)
def transition_lead_stage(
    lead_id: UUID,
    body: LeadStageCommand,
    context: Annotated[LeadCommandContext, Depends(get_lead_command_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> LeadStageCommandResult:
    if idempotency_key is None:
        raise HTTPException(status_code=422, detail="Invalid command")
    try:
        header_command_id = UUID(idempotency_key)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid command") from None
    if header_command_id != body.command_id:
        raise HTTPException(status_code=409, detail="Command conflict")
    principal = context.principal
    if principal.actor_id is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        with SqlAlchemyUnitOfWork(context.session_factory) as uow:
            result = HumanCommandService(uow).transition_lead(
                HumanCommandPrincipal(
                    actor_id=principal.actor_id,
                    workspace_id=principal.workspace_id,
                    permissions=principal.permissions,
                ),
                TransitionLeadCommand(
                    command_id=body.command_id,
                    workspace_id=principal.workspace_id,
                    lead_id=lead_id,
                    target_stage=body.target_stage,
                    expected_version=body.expected_version,
                    reviewed_correction=body.reviewed_correction,
                ),
            )
            uow.commit()
    except CommandAuthorizationError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except CommandConflictError:
        raise HTTPException(status_code=409, detail="Command conflict") from None
    return LeadStageCommandResult(
        command_id=result.command_id,
        lead_id=result.aggregate_id,
        version=result.version,
        replayed=result.replayed,
    )


@router.get("/leads", response_class=HTMLResponse)
def leads_page(
    request: Request,
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
):
    try:
        settings = get_settings()
        flags = get_feature_flags()
    except ValueError:
        settings = None
        flags = None
    request_origin = str(request.base_url).rstrip("/")
    write_ready = bool(
        settings is not None
        and flags is not None
        and flags.command_writer == "postgres"
        and settings.csrf_token
        and request_origin in settings.allowed_write_origins
        and context.principal.actor_id is not None
    )
    command_permissions = {
        "can_edit_lead": "crm:lead:edit",
        "can_transition_stage": "crm:lead-stage:write",
        "can_log_call": "crm:call:log",
        "can_log_email": "crm:email:log",
        "can_write_tasks": "crm:task:write",
    }
    capabilities = {
        name: write_ready and permission in context.principal.permissions
        for name, permission in command_permissions.items()
    }
    writable = any(capabilities.values())
    csrf_token = settings.csrf_token if settings is not None and writable else None
    return templates.TemplateResponse(
        request,
        "leads/index.html",
        {
            "request": request,
            "subject": context.principal.subject,
            "writable": writable,
            "csrf_token": csrf_token,
            **capabilities,
            "pipeline_queues": (
                ("calls_overdue", "Chamadas em atraso"),
                ("calls_today", "Chamadas hoje"),
                ("emails_overdue", "Emails em atraso"),
                ("emails_today", "Emails hoje"),
                ("proposal_followups_overdue", "Propostas em atraso"),
                ("proposal_followups_today", "Propostas hoje"),
                ("touched_today", "Contactados hoje"),
                ("untouched", "Sem contacto"),
                ("all", "Todos"),
            ),
        },
    )


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
