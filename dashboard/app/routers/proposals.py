from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, and_, case, func, select
from sqlalchemy.orm import Session

from dashboard.app.db import create_database_engine
from dashboard.app.feature_flags import require_proposals_postgres_reads
from dashboard.app.schemas.proposals import (
    DimensionTotals,
    ProposalDetail,
    ProposalFollowupReference,
    ProposalItemDetail,
    ProposalPage,
    ProposalPortfolio,
    ProposalSummary,
    ProposalVersionDetail,
    ValueStateCounts,
)
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    PROPOSAL_STATUSES,
    Account,
    Proposal,
    ProposalFollowup,
    ProposalItem,
    ProposalVersion,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))
_OPEN_STATUSES = ("draft", "promised", "sent", "viewed", "negotiation")
_DIMENSIONS = ("one_off", "mrr", "arr")


@dataclass(frozen=True)
class ProposalRequestContext:
    principal: CRMPrincipal
    session: Session


@lru_cache(maxsize=1)
def _proposal_engine() -> Engine:
    return create_database_engine()


def get_proposal_request_context(
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
):
    """Open the database only after identity and cutover gates pass."""

    require_proposals_postgres_reads()
    try:
        engine = _proposal_engine()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proposals unavailable",
        ) from None
    with Session(engine) as session:
        yield ProposalRequestContext(principal=principal, session=session)


def _age_expression():
    return case(
        (
            and_(
                Proposal.sent_at.is_not(None),
                Proposal.sent_verification_state == "verified",
            ),
            func.greatest(
                0,
                func.floor(
                    func.extract("epoch", func.now() - Proposal.sent_at) / 86400
                ),
            ),
        ),
        else_=None,
    )


def _summary_statement(workspace_id: UUID):
    followup_count = (
        select(func.count(ProposalFollowup.id))
        .where(ProposalFollowup.proposal_id == Proposal.id)
        .correlate(Proposal)
        .scalar_subquery()
    )
    last_interaction = (
        select(func.max(ProposalFollowup.occurred_at))
        .where(ProposalFollowup.proposal_id == Proposal.id)
        .correlate(Proposal)
        .scalar_subquery()
    )
    return (
        select(
            Proposal.id,
            Proposal.account_id,
            Account.display_name.label("account_name"),
            Account.commercial_vertical,
            Proposal.title,
            Proposal.status,
            Proposal.currency,
            Proposal.probability,
            Proposal.probability_source,
            Proposal.forecast_category,
            Proposal.next_action,
            Proposal.next_action_due_at,
            Proposal.owner_user_id.label("owner_id"),
            Proposal.value_state,
            ProposalVersion.one_off_amount,
            ProposalVersion.mrr_amount,
            ProposalVersion.arr_amount,
            Proposal.sent_at,
            Proposal.sent_verification_state,
            _age_expression().label("age_days"),
            followup_count.label("followup_count"),
            last_interaction.label("last_interaction_at"),
        )
        .join(
            Account,
            and_(
                Account.workspace_id == Proposal.workspace_id,
                Account.id == Proposal.account_id,
            ),
        )
        .outerjoin(
            ProposalVersion,
            ProposalVersion.id == Proposal.selected_version_id,
        )
        .where(Proposal.workspace_id == workspace_id)
    )


def _to_summary(row) -> ProposalSummary:
    return ProposalSummary(
        id=row.id,
        account_id=row.account_id,
        account_name=row.account_name,
        commercial_vertical=row.commercial_vertical,
        title=row.title,
        status=row.status,
        currency=row.currency.strip(),
        probability=row.probability,
        probability_source=row.probability_source,
        forecast_category=row.forecast_category,
        next_action=row.next_action,
        next_action_due_at=row.next_action_due_at,
        owner_id=row.owner_id,
        value_state=row.value_state,
        one_off_amount=row.one_off_amount,
        mrr_amount=row.mrr_amount,
        arr_amount=row.arr_amount,
        sent_at=row.sent_at,
        sent_verification_state=row.sent_verification_state,
        age_days=int(row.age_days) if row.age_days is not None else None,
        followup_count=row.followup_count,
        last_interaction_at=row.last_interaction_at,
    )


def _filtered_statement(
    workspace_id: UUID,
    *,
    proposal_status: str | None,
    account_id: UUID | None,
    owner_id: UUID | None,
    currency: str | None,
    age_min_days: int | None,
    age_max_days: int | None,
    next_action: str | None,
    forecast_category: str | None,
    commercial_vertical: str | None,
):
    statement = _summary_statement(workspace_id)
    filters = []
    if proposal_status is not None:
        filters.append(Proposal.status == proposal_status)
    if account_id is not None:
        filters.append(Proposal.account_id == account_id)
    if owner_id is not None:
        filters.append(Proposal.owner_user_id == owner_id)
    if currency is not None:
        filters.append(Proposal.currency == currency.upper())
    if age_min_days is not None:
        filters.append(_age_expression() >= age_min_days)
    if age_max_days is not None:
        filters.append(_age_expression() <= age_max_days)
    if next_action == "present":
        filters.append(Proposal.next_action.is_not(None))
    elif next_action == "missing":
        filters.append(Proposal.next_action.is_(None))
    elif next_action == "due":
        filters.append(Proposal.next_action_due_at <= datetime.now(UTC))
    if forecast_category is not None:
        filters.append(Proposal.forecast_category == forecast_category)
    if commercial_vertical is not None:
        filters.append(Account.commercial_vertical == commercial_vertical)
    return statement.where(*filters)


@router.get("/api/v1/proposals/portfolio", response_model=ProposalPortfolio)
def proposal_portfolio(
    context: Annotated[ProposalRequestContext, Depends(get_proposal_request_context)],
) -> ProposalPortfolio:
    rows = context.session.execute(
        select(
            Proposal.status,
            Proposal.currency,
            Proposal.value_state,
            Proposal.probability,
            Proposal.probability_source,
            ProposalVersion.one_off_amount,
            ProposalVersion.mrr_amount,
            ProposalVersion.arr_amount,
        )
        .outerjoin(
            ProposalVersion,
            ProposalVersion.id == Proposal.selected_version_id,
        )
        .where(Proposal.workspace_id == context.principal.workspace_id)
    ).all()
    states = Counter(row.value_state for row in rows)
    statuses = Counter(row.status for row in rows)
    buckets: dict[str, dict[str, dict[str, Decimal]]] = {
        name: {} for name in ("totals", "open", "weighted", "won", "lost")
    }

    def add(bucket: str, currency: str, row, factor: Decimal = Decimal("1")):
        values = buckets[bucket].setdefault(
            currency, {dimension: Decimal("0.00") for dimension in _DIMENSIONS}
        )
        for dimension in _DIMENSIONS:
            amount = getattr(row, f"{dimension}_amount")
            if amount is not None:
                values[dimension] += (amount * factor).quantize(Decimal("0.01"))

    for row in rows:
        if row.value_state != "confirmed":
            continue
        currency = row.currency.strip()
        add("totals", currency, row)
        if row.status in _OPEN_STATUSES:
            add("open", currency, row)
            if row.probability is not None and row.probability_source is not None:
                add("weighted", currency, row, row.probability / Decimal("100"))
        elif row.status == "won":
            add("won", currency, row)
        elif row.status == "lost":
            add("lost", currency, row)

    def convert(name: str) -> dict[str, DimensionTotals]:
        return {
            currency: DimensionTotals(**amounts)
            for currency, amounts in sorted(buckets[name].items())
        }

    return ProposalPortfolio(
        proposal_count=len(rows),
        value_counts=ValueStateCounts(
            missing=states["missing"],
            candidate=states["candidate"],
            confirmed=states["confirmed"],
            rejected=states["rejected"],
        ),
        status_counts=dict(sorted(statuses.items())),
        totals=convert("totals"),
        open_pipeline=convert("open"),
        weighted_pipeline=convert("weighted"),
        won_totals=convert("won"),
        lost_totals=convert("lost"),
    )


@router.get("/api/v1/proposals", response_model=ProposalPage)
def list_proposals(
    context: Annotated[ProposalRequestContext, Depends(get_proposal_request_context)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[
        Literal[*PROPOSAL_STATUSES] | None, Query(alias="status")
    ] = None,
    account_id: UUID | None = None,
    owner_id: UUID | None = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Za-z]{3}$")] = None,
    age_min_days: Annotated[int | None, Query(ge=0)] = None,
    age_max_days: Annotated[int | None, Query(ge=0)] = None,
    next_action: Literal["present", "missing", "due"] | None = None,
    forecast_category: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    commercial_vertical: Annotated[
        str | None, Query(min_length=1, max_length=255)
    ] = None,
) -> ProposalPage:
    if (
        age_min_days is not None
        and age_max_days is not None
        and age_min_days > age_max_days
    ):
        raise HTTPException(status_code=422, detail="Invalid age range")
    statement = _filtered_statement(
        context.principal.workspace_id,
        proposal_status=status_filter,
        account_id=account_id,
        owner_id=owner_id,
        currency=currency,
        age_min_days=age_min_days,
        age_max_days=age_max_days,
        next_action=next_action,
        forecast_category=forecast_category,
        commercial_vertical=commercial_vertical,
    )
    total = context.session.scalar(
        select(func.count()).select_from(statement.subquery())
    )
    rows = context.session.execute(
        statement.order_by(Proposal.updated_at.desc(), Proposal.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    return ProposalPage(
        items=tuple(_to_summary(row) for row in rows),
        total=total or 0,
        limit=limit,
        offset=offset,
    )


def _proposal_or_404(context: ProposalRequestContext, proposal_id: UUID):
    row = context.session.execute(
        _summary_statement(context.principal.workspace_id).where(
            Proposal.id == proposal_id
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return row


@router.get("/api/v1/proposals/{proposal_id}", response_model=ProposalDetail)
def proposal_detail(
    proposal_id: UUID,
    context: Annotated[ProposalRequestContext, Depends(get_proposal_request_context)],
) -> ProposalDetail:
    summary_row = _proposal_or_404(context, proposal_id)
    proposal = context.session.execute(
        select(
            Proposal.sent_evidence_id,
            Proposal.lost_reason,
            Proposal.won_at,
            Proposal.lost_at,
        ).where(
            Proposal.workspace_id == context.principal.workspace_id,
            Proposal.id == proposal_id,
        )
    ).one()
    version_rows = (
        context.session.execute(
            select(ProposalVersion)
            .where(ProposalVersion.proposal_id == proposal_id)
            .order_by(ProposalVersion.version_number.desc())
        )
        .scalars()
        .all()
    )
    version_ids = [version.id for version in version_rows]
    item_rows = (
        context.session.execute(
            select(ProposalItem)
            .where(ProposalItem.proposal_version_id.in_(version_ids))
            .order_by(ProposalItem.id)
        )
        .scalars()
        .all()
        if version_ids
        else []
    )
    items_by_version: dict[UUID, list[ProposalItemDetail]] = {
        version_id: [] for version_id in version_ids
    }
    for item in item_rows:
        items_by_version[item.proposal_version_id].append(
            ProposalItemDetail(
                id=item.id,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                billing_period=item.billing_period,
                option_group=item.option_group,
                is_selected=item.is_selected,
                amount=item.amount,
                currency=item.currency.strip(),
            )
        )
    followups = (
        context.session.execute(
            select(ProposalFollowup)
            .where(ProposalFollowup.proposal_id == proposal_id)
            .order_by(ProposalFollowup.sequence_number.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    return ProposalDetail(
        **_to_summary(summary_row).model_dump(),
        sent_evidence_id=proposal.sent_evidence_id,
        lost_reason=proposal.lost_reason,
        won_at=proposal.won_at,
        lost_at=proposal.lost_at,
        versions=tuple(
            ProposalVersionDetail(
                id=version.id,
                version_number=version.version_number,
                status=version.status,
                created_at=version.created_at,
                sent_at=version.sent_at,
                valid_until=version.valid_until,
                one_off_amount=version.one_off_amount,
                mrr_amount=version.mrr_amount,
                arr_amount=version.arr_amount,
                tax_inclusion=version.tax_inclusion,
                source_document_evidence_id=version.source_document_evidence_id,
                extraction_confidence=version.extraction_confidence,
                confirmed_at=version.confirmed_at,
                items=tuple(items_by_version[version.id]),
            )
            for version in version_rows
        ),
        followups=tuple(
            ProposalFollowupReference(
                id=followup.id,
                activity_id=followup.activity_id,
                sequence_number=followup.sequence_number,
                occurred_at=followup.occurred_at,
                channel=followup.channel,
            )
            for followup in followups
        ),
    )


@router.get("/propostas", response_class=HTMLResponse)
def proposals_page(
    request: Request,
    context: Annotated[ProposalRequestContext, Depends(get_proposal_request_context)],
):
    return templates.TemplateResponse(
        request,
        "proposals/index.html",
        {"request": request, "subject": context.principal.subject},
    )


@router.get("/propostas/{proposal_id}", response_class=HTMLResponse)
def proposal_page(
    request: Request,
    proposal_id: UUID,
    context: Annotated[ProposalRequestContext, Depends(get_proposal_request_context)],
):
    _proposal_or_404(context, proposal_id)
    return templates.TemplateResponse(
        request,
        "proposals/detail.html",
        {
            "request": request,
            "proposal_id": str(proposal_id),
            "subject": context.principal.subject,
        },
    )
