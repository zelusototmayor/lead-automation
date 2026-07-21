from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, func, literal, or_, select

from dashboard.app.routers.accounts import (
    AccountRequestContext,
    get_account_request_context,
)
from dashboard.app.schemas.pipeline import (
    LeadDetail,
    LeadTask,
    LeadTaskPage,
    PipelineItem,
    PipelinePage,
    PipelinePriority,
    PipelineQueue,
    PipelineSummary,
    PipelineTask,
    TimelineItem,
    TimelinePage,
)
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    Lead,
    Task,
    Workspace,
)

router = APIRouter()

_QUEUES: tuple[PipelineQueue, ...] = (
    "calls_overdue",
    "calls_today",
    "calls_future",
    "emails_overdue",
    "emails_today",
    "emails_future",
    "proposal_followups_overdue",
    "proposal_followups_today",
    "touched_today",
    "untouched",
    "all",
)
_TASK_QUEUES = frozenset(
    queue
    for queue in _QUEUES
    if queue.startswith(("calls_", "emails_", "proposal_followups_"))
)
_QUALIFYING_ACTIVITY_TYPES = (
    "call",
    "email_sent",
    "email_received",
    "meeting",
    "proposal",
    "stage_change",
    "note",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _day_bounds(
    context: AccountRequestContext, now: datetime
) -> tuple[datetime, datetime]:
    timezone_name = context.session.scalar(
        select(Workspace.timezone).where(Workspace.id == context.principal.workspace_id)
    )
    try:
        timezone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pipeline unavailable",
        ) from None
    local_date = now.astimezone(timezone).date()
    start = datetime.combine(local_date, time.min, timezone).astimezone(UTC)
    end = datetime.combine(local_date, time.max, timezone).astimezone(UTC)
    return start, end


def _task_filter(queue: PipelineQueue, start: datetime, end: datetime):
    if queue.startswith("calls_"):
        type_filter = Task.task_type == "call"
    elif queue.startswith("emails_"):
        type_filter = Task.task_type == "email"
    else:
        type_filter = and_(
            Task.proposal_id.is_not(None),
            Task.task_type.in_(("follow_up", "proposal_followup")),
        )
    if queue.endswith("_overdue"):
        due_filter = Task.due_at < start
    elif queue.endswith("_future"):
        due_filter = Task.due_at > end
    else:
        due_filter = Task.due_at.between(start, end)
    return and_(Task.status == "open", type_filter, due_filter)


def _activity_exists(
    workspace_id, start: datetime | None = None, end: datetime | None = None
):
    conditions = [
        Activity.workspace_id == workspace_id,
        Activity.lead_id == Lead.id,
        Activity.activity_type.in_(_QUALIFYING_ACTIVITY_TYPES),
    ]
    if start is not None:
        conditions.append(Activity.occurred_at >= start)
    if end is not None:
        conditions.append(Activity.occurred_at <= end)
    return exists(select(Activity.id).where(*conditions))


def _pipeline_statement(
    workspace_id, queue: PipelineQueue, start: datetime, end: datetime
):
    base_columns = (
        Lead.id.label("lead_id"),
        Lead.account_id,
        func.coalesce(Account.display_name, literal("Sem conta")).label("company"),
        Contact.full_name.label("contact_name"),
        Contact.primary_email.label("email"),
        Contact.phone,
        Account.city,
        Lead.stage,
        Lead.priority,
        Lead.version.label("lead_version"),
    )
    statement = (
        select(*base_columns)
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
        .where(Lead.workspace_id == workspace_id)
    )
    if queue in _TASK_QUEUES:
        return statement.add_columns(
            Task.id.label("task_id"),
            Task.task_type.label("task_type"),
            Task.title.label("task_title"),
            Task.due_at.label("task_due_at"),
            Task.version.label("task_version"),
        ).join(
            Task,
            (Task.workspace_id == Lead.workspace_id)
            & (Task.lead_id == Lead.id)
            & _task_filter(queue, start, end),
        )
    if queue == "touched_today":
        statement = statement.where(_activity_exists(workspace_id, start, end))
    elif queue == "untouched":
        statement = statement.where(~_activity_exists(workspace_id))
    next_task = (
        select(Task)
        .where(
            Task.workspace_id == workspace_id,
            Task.lead_id == Lead.id,
            Task.status == "open",
        )
        .order_by(Task.due_at.asc(), Task.id.asc())
        .limit(1)
        .lateral()
    )
    return statement.add_columns(
        next_task.c.id.label("task_id"),
        next_task.c.task_type.label("task_type"),
        next_task.c.title.label("task_title"),
        next_task.c.due_at.label("task_due_at"),
        next_task.c.version.label("task_version"),
    ).outerjoin(next_task, literal(True))


def _redact_phone(phone: str | None) -> str | None:
    if phone is None:
        return None
    prefix = "+" if phone.startswith("+") else ""
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) < 8:
        return None
    country_prefix = digits[:3] if prefix else ""
    return f"{prefix}{country_prefix}****{digits[-4:]}"


def _to_item(row) -> PipelineItem:
    task = None
    if row.task_id is not None:
        task = PipelineTask(
            id=row.task_id,
            type=row.task_type,
            title=row.task_title,
            due_at=row.task_due_at,
            version=row.task_version,
        )
    return PipelineItem(
        lead_id=row.lead_id,
        account_id=row.account_id,
        company=row.company,
        contact_name=row.contact_name,
        email=str(row.email) if row.email is not None else None,
        phone=_redact_phone(row.phone),
        city=row.city,
        stage=row.stage,
        priority=row.priority,
        lead_version=row.lead_version,
        task=task,
    )


@router.get("/api/v1/pipeline/summary", response_model=PipelineSummary)
def pipeline_summary(
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
) -> PipelineSummary:
    now = _utc_now()
    start, end = _day_bounds(context, now)
    counts: dict[PipelineQueue, int] = {}
    for queue in _QUEUES:
        rows = _pipeline_statement(
            context.principal.workspace_id, queue, start, end
        ).subquery()
        counts[queue] = int(
            context.session.scalar(select(func.count(func.distinct(rows.c.lead_id))))
            or 0
        )
    return PipelineSummary(queues=counts, generated_at=now)


def _literal_search_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/api/v1/pipeline/items", response_model=PipelinePage)
def pipeline_items(
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
    queue: Annotated[PipelineQueue, Query()] = "all",
    priority: Annotated[PipelinePriority | None, Query()] = None,
    search: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=200,
            pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PipelinePage:
    start, end = _day_bounds(context, _utc_now())
    statement = _pipeline_statement(context.principal.workspace_id, queue, start, end)
    if priority is not None:
        statement = statement.where(Lead.priority == priority)
    if search is not None:
        pattern = _literal_search_pattern(search)
        statement = statement.where(
            or_(
                Account.display_name.ilike(pattern, escape="\\"),
                Contact.full_name.ilike(pattern, escape="\\"),
                Contact.primary_email.ilike(pattern, escape="\\"),
                Contact.phone.ilike(pattern, escape="\\"),
                Account.city.ilike(pattern, escape="\\"),
            )
        )
    rows = statement.subquery()
    total = int(context.session.scalar(select(func.count()).select_from(rows)) or 0)
    page_rows = context.session.execute(
        statement.order_by(
            Task.due_at.asc().nulls_last()
            if queue in _TASK_QUEUES
            else Lead.updated_at.desc(),
            Task.id.asc() if queue in _TASK_QUEUES else Lead.id.asc(),
            Lead.id.asc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()
    return PipelinePage(
        queue=queue,
        items=tuple(_to_item(row) for row in page_rows),
        total=total,
        limit=limit,
        offset=offset,
    )


def _lead_detail_row(context: AccountRequestContext, lead_id: UUID):
    row = context.session.execute(
        select(
            Lead.id,
            Lead.account_id,
            func.coalesce(Account.display_name, literal("Sem conta")).label("company"),
            Contact.full_name.label("contact_name"),
            Contact.primary_email.label("email"),
            Contact.phone,
            Account.city,
            Lead.stage,
            Lead.priority,
            Lead.version,
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
        .where(
            Lead.workspace_id == context.principal.workspace_id,
            Lead.id == lead_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return row


@router.get("/api/v1/leads/{lead_id}", response_model=LeadDetail)
def lead_detail(
    lead_id: UUID,
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
) -> LeadDetail:
    row = _lead_detail_row(context, lead_id)
    return LeadDetail(
        id=row.id,
        account_id=row.account_id,
        company=row.company,
        contact_name=row.contact_name,
        email=str(row.email) if row.email is not None else None,
        phone=str(row.phone) if row.phone is not None else None,
        city=row.city,
        stage=row.stage,
        priority=row.priority,
        version=row.version,
    )


@router.get("/api/v1/leads/{lead_id}/timeline", response_model=TimelinePage)
def lead_timeline(
    lead_id: UUID,
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TimelinePage:
    _lead_detail_row(context, lead_id)
    filters = (
        Activity.workspace_id == context.principal.workspace_id,
        Activity.lead_id == lead_id,
    )
    total = int(
        context.session.scalar(select(func.count(Activity.id)).where(*filters)) or 0
    )
    rows = context.session.execute(
        select(
            Activity.id,
            Activity.activity_type,
            Activity.title,
            Activity.summary,
            Activity.outcome_code,
            Activity.direction,
            Activity.occurred_at,
        )
        .where(*filters)
        .order_by(Activity.occurred_at.desc(), Activity.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return TimelinePage(
        items=tuple(
            TimelineItem(
                id=row.id,
                type=row.activity_type,
                title=row.title,
                summary=row.summary,
                outcome_code=row.outcome_code,
                direction=row.direction,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/leads/{lead_id}/tasks", response_model=LeadTaskPage)
def lead_tasks(
    lead_id: UUID,
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeadTaskPage:
    _lead_detail_row(context, lead_id)
    filters = (
        Task.workspace_id == context.principal.workspace_id,
        Task.lead_id == lead_id,
    )
    total = int(
        context.session.scalar(select(func.count(Task.id)).where(*filters)) or 0
    )
    rows = context.session.execute(
        select(Task)
        .where(*filters)
        .order_by(Task.due_at.asc(), Task.id.asc())
        .limit(limit)
        .offset(offset)
    ).scalars()
    return LeadTaskPage(
        items=tuple(
            LeadTask(
                id=row.id,
                type=row.task_type,
                title=row.title,
                due_at=row.due_at,
                status=row.status,
                version=row.version,
            )
            for row in rows
        ),
        total=total,
        limit=limit,
        offset=offset,
    )
