from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

from dashboard.app.db import create_database_engine
from dashboard.app.feature_flags import require_database_enabled
from dashboard.app.schemas.intelligence import RecommendationPage, RecommendationSummary
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import Account, Recommendation


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))


@dataclass(frozen=True)
class IntelligenceRequestContext:
    principal: CRMPrincipal
    session: Session


@lru_cache(maxsize=1)
def _intelligence_engine() -> Engine:
    return create_database_engine()


def get_intelligence_request_context(
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
):
    require_database_enabled(detail="Intelligence unavailable")
    try:
        engine = _intelligence_engine()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Intelligence unavailable",
        ) from None
    with Session(engine) as session:
        yield IntelligenceRequestContext(principal, session)


def _statement(workspace_id: UUID):
    return (
        select(Recommendation, Account.display_name.label("account_name"))
        .join(
            Account,
            (Account.workspace_id == Recommendation.workspace_id)
            & (Account.id == Recommendation.account_id),
        )
        .where(Recommendation.workspace_id == workspace_id)
    )


def _summary(row) -> RecommendationSummary:
    recommendation, account_name = row
    return RecommendationSummary(
        id=recommendation.id,
        account_id=recommendation.account_id,
        account_name=account_name,
        proposal_id=recommendation.proposal_id,
        rule_code=recommendation.rule_code,
        priority=recommendation.priority,
        evidence=tuple(recommendation.evidence_json),
        state=recommendation.state,
        observed_at=recommendation.observed_at,
    )


@router.get("/api/v1/intelligence/recommendations", response_model=RecommendationPage)
def recommendations(
    context: Annotated[
        IntelligenceRequestContext, Depends(get_intelligence_request_context)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    priority: Literal["critical", "high", "medium", "low"] | None = None,
    state: Literal["open", "resolved", "dismissed"] = "open",
) -> RecommendationPage:
    statement = _statement(context.principal.workspace_id).where(
        Recommendation.state == state
    )
    if priority is not None:
        statement = statement.where(Recommendation.priority == priority)
    total = (
        context.session.scalar(select(func.count()).select_from(statement.subquery()))
        or 0
    )
    rows = context.session.execute(
        statement.order_by(
            case(
                (Recommendation.priority == "critical", 0),
                (Recommendation.priority == "high", 1),
                (Recommendation.priority == "medium", 2),
                else_=3,
            ),
            Recommendation.observed_at.desc(),
            Recommendation.id,
        )
        .limit(limit)
        .offset(offset)
    ).all()
    return RecommendationPage(
        items=tuple(_summary(row) for row in rows),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/intelligence/recommendations/{recommendation_id}",
    response_model=RecommendationSummary,
)
def recommendation_detail(
    recommendation_id: UUID,
    context: Annotated[
        IntelligenceRequestContext, Depends(get_intelligence_request_context)
    ],
) -> RecommendationSummary:
    row = context.session.execute(
        _statement(context.principal.workspace_id).where(
            Recommendation.id == recommendation_id
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return _summary(row)


@router.get("/inteligencia", response_class=HTMLResponse)
def intelligence_page(
    request: Request,
    context: Annotated[
        IntelligenceRequestContext, Depends(get_intelligence_request_context)
    ],
):
    del context
    return templates.TemplateResponse(
        request=request, name="intelligence/index.html", context={}
    )
