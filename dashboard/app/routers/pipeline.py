from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, and_, case, exists, func, literal, select, union_all

from dashboard.app.routers.accounts import (
    AccountRequestContext,
    get_account_request_context,
)
from dashboard.app.schemas.pipeline import (
    AnalyticsCountBreakdown,
    AnalyticsDay,
    AnalyticsPeriod,
    AnalyticsQueueBreakdown,
    AnalyticsTaskBreakdown,
    LeadDetail,
    LeadTask,
    LeadTaskPage,
    PipelineAnalytics,
    PipelineItem,
    PipelinePage,
    PipelinePriority,
    PipelineQueue,
    PipelineQueueUnit,
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
    Proposal,
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
_QUEUE_UNITS: dict[PipelineQueue, PipelineQueueUnit] = {
    queue: "task" if queue in _TASK_QUEUES else "lead" for queue in _QUEUES
}
_QUALIFYING_ACTIVITY_TYPES = (
    "call",
    "email_sent",
    "email_received",
    "meeting",
    "proposal",
    "stage_change",
    "note",
)
_ANALYTICS_OUTCOMES = (
    "connected",
    "no_answer",
    "voicemail",
    "wrong_number",
    "not_interested",
    "follow_up",
)
_ANALYTICS_TASK_TYPES = ("call", "email", "follow_up", "proposal_followup")
_ANALYTICS_QUEUE_NAMES: tuple[PipelineQueue, ...] = (
    "calls_overdue",
    "calls_today",
    "emails_overdue",
    "emails_today",
    "proposal_followups_overdue",
    "proposal_followups_today",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _workspace_timezone(context: AccountRequestContext) -> tuple[str, ZoneInfo]:
    timezone_name = context.session.scalar(
        select(Workspace.timezone).where(Workspace.id == context.principal.workspace_id)
    )
    try:
        return timezone_name, ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pipeline unavailable",
        ) from None


def _day_bounds(
    context: AccountRequestContext, now: datetime
) -> tuple[datetime, datetime]:
    _, timezone = _workspace_timezone(context)
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
        count_expression = (
            func.count()
            if queue in _TASK_QUEUES
            else func.count(func.distinct(rows.c.lead_id))
        )
        counts[queue] = int(
            context.session.scalar(select(count_expression).select_from(rows)) or 0
        )
    return PipelineSummary(queues=counts, queue_units=_QUEUE_UNITS, generated_at=now)


@router.get("/api/v1/pipeline/analytics", response_model=PipelineAnalytics)
def pipeline_analytics(
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
    days: Annotated[int, Query(ge=1, le=120)] = 30,
) -> PipelineAnalytics:
    now = _utc_now()
    workspace_id = context.principal.workspace_id
    timezone_name, timezone = _workspace_timezone(context)
    end_date = now.astimezone(timezone).date()
    start_date = end_date - timedelta(days=days - 1)
    start_at = datetime.combine(start_date, time.min, timezone).astimezone(UTC)
    end_at = datetime.combine(
        end_date + timedelta(days=1), time.min, timezone
    ).astimezone(UTC)
    local_day = func.date(func.timezone(timezone_name, Activity.occurred_at))
    outcome_bucket = case(
        (Activity.outcome_code.is_(None), None),
        (
            Activity.outcome_code.in_(_ANALYTICS_OUTCOMES),
            Activity.outcome_code,
        ),
        else_="other",
    ).cast(String)
    activity_filters = (
        Activity.workspace_id == workspace_id,
        Activity.occurred_at >= start_at,
        Activity.occurred_at < end_at,
        Activity.activity_type.in_(_QUALIFYING_ACTIVITY_TYPES),
    )
    activity_groups = (
        select(
            literal("activity").label("metric"),
            local_day.label("local_day"),
            Activity.activity_type.label("dimension"),
            outcome_bucket.label("outcome"),
            func.count().label("value"),
        )
        .where(*activity_filters)
        .group_by(local_day, Activity.activity_type, outcome_bucket)
    )
    touched_groups = (
        select(
            literal("touched").label("metric"),
            local_day.label("local_day"),
            literal(None).cast(String).label("dimension"),
            literal(None).cast(String).label("outcome"),
            func.count(func.distinct(Activity.lead_id)).label("value"),
        )
        .where(*activity_filters, Activity.lead_id.is_not(None))
        .group_by(local_day)
    )
    activity_rows = context.session.execute(
        union_all(activity_groups, touched_groups)
    ).all()

    daily_values: dict[date, dict[str, object]] = {
        start_date + timedelta(days=offset): {
            "activity_types": {},
            "outcomes": {},
            "distinct_touched_leads": 0,
        }
        for offset in range(days)
    }
    for row in activity_rows:
        values = daily_values[row.local_day]
        if row.metric == "touched":
            values["distinct_touched_leads"] = int(row.value)
            continue
        activity_types = values["activity_types"]
        outcomes = values["outcomes"]
        assert isinstance(activity_types, dict)
        assert isinstance(outcomes, dict)
        activity_types[row.dimension] = activity_types.get(row.dimension, 0) + int(
            row.value
        )
        if row.outcome is not None:
            outcomes[row.outcome] = outcomes.get(row.outcome, 0) + int(row.value)

    stage_rows = context.session.execute(
        select(Lead.stage, func.count())
        .where(Lead.workspace_id == workspace_id)
        .group_by(Lead.stage)
    ).all()
    stage_counts = {row.stage: int(row.count) for row in stage_rows}

    proposal_rows = context.session.execute(
        select(Proposal.status, func.count())
        .where(Proposal.workspace_id == workspace_id)
        .group_by(Proposal.status)
    ).all()
    proposal_counts = {row.status: int(row.count) for row in proposal_rows}

    task_type_bucket = case(
        (Task.task_type.in_(_ANALYTICS_TASK_TYPES), Task.task_type), else_="other"
    ).cast(String)
    task_status_groups = (
        select(
            literal("status").label("metric"),
            Task.status.label("dimension"),
            func.count().label("value"),
        )
        .where(Task.workspace_id == workspace_id)
        .group_by(Task.status)
    )
    open_task_type_groups = (
        select(
            literal("open_type").label("metric"),
            task_type_bucket.label("dimension"),
            func.count().label("value"),
        )
        .where(Task.workspace_id == workspace_id, Task.status == "open")
        .group_by(task_type_bucket)
    )
    task_rows = context.session.execute(
        union_all(task_status_groups, open_task_type_groups)
    ).all()
    task_status_counts = {
        row.dimension: int(row.value) for row in task_rows if row.metric == "status"
    }
    open_task_type_counts = {
        row.dimension: int(row.value) for row in task_rows if row.metric == "open_type"
    }

    today_start = datetime.combine(end_date, time.min, timezone).astimezone(UTC)
    today_end = datetime.combine(end_date, time.max, timezone).astimezone(UTC)
    queue_row = context.session.execute(
        select(
            *(
                func.count()
                .filter(_task_filter(queue, today_start, today_end))
                .label(queue)
                for queue in _ANALYTICS_QUEUE_NAMES
            )
        ).where(Task.workspace_id == workspace_id)
    ).one()
    queue_counts = {
        queue: int(getattr(queue_row, queue)) for queue in _ANALYTICS_QUEUE_NAMES
    }

    return PipelineAnalytics(
        period=AnalyticsPeriod(start_date=start_date, end_date=end_date, days=days),
        daily=tuple(
            AnalyticsDay(date=local_date, **values)
            for local_date, values in daily_values.items()
        ),
        stages=AnalyticsCountBreakdown(
            by_status=stage_counts, total=sum(stage_counts.values())
        ),
        proposals=AnalyticsCountBreakdown(
            by_status=proposal_counts, total=sum(proposal_counts.values())
        ),
        tasks=AnalyticsTaskBreakdown(
            by_status=task_status_counts,
            open_by_type=open_task_type_counts,
            total=sum(task_status_counts.values()),
        ),
        queues=AnalyticsQueueBreakdown(counts=queue_counts),
        generated_at=now,
    )


@router.get("/api/v1/pipeline/items", response_model=PipelinePage)
def pipeline_items(
    context: Annotated[AccountRequestContext, Depends(get_account_request_context)],
    queue: Annotated[PipelineQueue, Query()] = "all",
    priority: Annotated[PipelinePriority | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PipelinePage:
    start, end = _day_bounds(context, _utc_now())
    statement = _pipeline_statement(context.principal.workspace_id, queue, start, end)
    if priority is not None:
        statement = statement.where(Lead.priority == priority)
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
