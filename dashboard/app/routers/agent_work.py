"""Private automation work API; browser reads remain under CRM principal auth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
import secrets
from typing import Annotated, Any, Literal
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, case
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from dashboard.app.db import create_database_engine
from dashboard.app.feature_flags import require_postgres_command_writer
from dashboard.app.security import CRMPrincipal, require_crm_principal
from dashboard.app.routers.agent_events import _consume_rate_limit
from src.crm.persistence.models import (
    AgentWork,
    Lead,
    Task,
    Activity,
    AuditEvent,
    Account,
    Contact,
)
from src.crm.services.agent_work_service import (
    claim_work,
    enqueue_task_work,
    lead_is_suppressed,
    fail_work,
    finish_work,
    resolve_waiting_review,
    locked_work,
    serialize_work,
    validate_lease,
    WorkConflict,
)
from src.crm.services.callback_execution import execute_callback
from src.crm.services.note_status_service import with_note_processing

router = APIRouter()


@dataclass(frozen=True)
class AutomationPrincipal:
    workspace_id: UUID
    actor_id: UUID
    scopes: frozenset[str]
    source_scopes: frozenset[str]


def require_automation_principal(request: Request) -> AutomationPrincipal:
    """Configured scoped service credential; never accepts browser cookies or tenant input."""
    try:
        token = os.environ["CRM_AUTOMATION_BEARER_TOKEN"]
        workspace_id = UUID(os.environ["CRM_AUTOMATION_WORKSPACE_ID"])
        scopes = frozenset(os.environ["CRM_AUTOMATION_SCOPES"].split(","))
        issued = datetime.fromisoformat(
            request.headers.get("x-agent-timestamp", "").replace("Z", "+00:00")
        )
        authorization = request.headers.get("authorization", "")
        if (
            len(token) < 32
            or not secrets.compare_digest(authorization, "Bearer " + token)
            or issued.tzinfo is None
            or abs(datetime.now(UTC) - issued) > timedelta(minutes=5)
        ):
            raise ValueError
        if request.headers.get("origin"):
            raise ValueError
    except (KeyError, ValueError, TypeError, UnicodeEncodeError):
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    if not _consume_rate_limit(workspace_id, "automation"):
        raise HTTPException(
            status_code=429, detail="Too many requests", headers={"Retry-After": "60"}
        )
    return AutomationPrincipal(
        workspace_id,
        uuid5(workspace_id, "head-of-sales"),
        scopes,
        frozenset(
            filter(None, os.environ.get("CRM_AUTOMATION_SOURCE_SCOPES", "").split(","))
        ),
    )


def require_scope(principal, scope):
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail="Forbidden")
    require_postgres_command_writer()


def get_work_session():
    engine = create_database_engine()
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


Principal = Annotated[AutomationPrincipal, Depends(require_automation_principal)]
Database = Annotated[Session, Depends(get_work_session)]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimBody(StrictBody):
    worker_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=20)
    lease_seconds: int = Field(default=300, ge=30, le=600)
    kinds: (
        list[
            Literal[
                "calendar_callback",
                "call_followup",
                "reply_review",
                "proposal_review",
                "identity_review",
                "sent_review",
                "calendar_review",
                "followup_due",
            ]
        ]
        | None
    ) = Field(default=None, min_length=1, max_length=8)


class LeaseBody(StrictBody):
    lease_token: UUID


class WorkResult(StrictBody):
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    next_action: dict[str, Any] | None = None


class FinishBody(LeaseBody):
    result: WorkResult
    status: Literal["completed", "waiting"] = "completed"


class ReviewResolutionResult(StrictBody):
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[dict[str, Any]] = Field(min_length=1, max_length=20)


class ResolveReviewBody(StrictBody):
    resolution_id: UUID
    expected_result_hash: str = Field(pattern="^[0-9a-f]{64}$")
    result: ReviewResolutionResult


class FailBody(LeaseBody):
    reason: str = Field(default="Execution failed", min_length=1, max_length=256)
    retryable: bool = True


class NextActionBody(LeaseBody):
    expected_lead_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    due_at: AwareDatetime
    task_type: Literal["email", "follow_up"] = "follow_up"
    supersede_agent_followups: bool = False


def _list(session, workspace, status, limit):
    query = select(AgentWork).where(
        AgentWork.workspace_id == workspace,
        AgentWork.kind.notin_(("bulk_observation", "unmatched_observation")),
    )
    if status:
        query = query.where(AgentWork.status == status)
    return [
        with_note_processing(session, workspace, serialize_work(item))
        for item in session.scalars(
            query.order_by(
                case(
                    {
                        "running": 0,
                        "waiting": 1,
                        "failed": 2,
                        "queued": 3,
                        "completed": 4,
                    },
                    value=AgentWork.status,
                    else_=5,
                ),
                AgentWork.updated_at.desc(),
            ).limit(limit)
        )
    ]


@router.get("/api/v1/agent/work")
def list_work(
    principal: Principal,
    session: Database,
    status: Literal["queued", "running", "waiting", "completed", "failed"]
    | None = None,
    limit: int = 20,
):
    require_scope(principal, "work:read")
    return {
        "items": _list(session, principal.workspace_id, status, max(1, min(limit, 100)))
    }


@router.get("/api/v1/agent/work/{work_id}")
def read_work(work_id: UUID, principal: Principal, session: Database):
    require_scope(principal, "work:read")
    row = session.scalar(
        select(AgentWork).where(
            AgentWork.workspace_id == principal.workspace_id,
            AgentWork.id == work_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Work not found")
    return _context(session, principal.workspace_id, serialize_work(row))


@router.get("/api/v1/agent-work")
def human_work(
    session: Database,
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
    limit: int = 50,
):
    return {
        "items": _list(session, principal.workspace_id, None, max(1, min(limit, 100)))
    }


def _context(session, workspace_id, item):
    if item["lead_id"]:
        lead = session.scalar(
            select(Lead).where(
                Lead.workspace_id == workspace_id, Lead.id == UUID(item["lead_id"])
            )
        )
        if lead:
            account = session.get(Account, lead.account_id) if lead.account_id else None
            contact = session.get(Contact, lead.contact_id) if lead.contact_id else None
            item["context"] = {
                "lead_version": lead.version,
                "suppressed": lead_is_suppressed(session, lead),
                "company": account.display_name if account else lead.company_name,
                "contact_name": contact.full_name if contact else lead.contact_name,
                "contact_email": str(contact.primary_email)
                if contact and contact.primary_email
                else lead.contact_email,
                "stage": lead.stage,
                "open_tasks": [
                    {
                        "id": str(t.id),
                        "type": t.task_type,
                        "title": t.title,
                        "due_at": t.due_at.isoformat(),
                        "source_rule": t.source_rule,
                        "version": t.version,
                        "status": t.status,
                    }
                    for t in session.scalars(
                        select(Task)
                        .where(
                            Task.workspace_id == workspace_id,
                            Task.lead_id == lead.id,
                            Task.status == "open",
                        )
                        .limit(30)
                    )
                ],
            }
            if item["kind"] == "call_followup":
                from src.crm.services.note_source_service import note_context
                item["context"]["note_source"] = note_context(
                    session, workspace_id, lead.id, item["payload"]
                )
    return with_note_processing(session, workspace_id, item)


@router.post("/api/v1/agent/work/claim")
def claim(body: ClaimBody, principal: Principal, session: Database):
    require_scope(principal, "work:write")
    with session.begin():
        items = claim_work(session, principal.workspace_id, **body.model_dump())
        return {
            "items": [_context(session, principal.workspace_id, item) for item in items]
        }


@router.post("/api/v1/agent/work/{work_id}/finish")
def finish(work_id: UUID, body: FinishBody, principal: Principal, session: Database):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            row = locked_work(session, principal.workspace_id, work_id)
            if row.kind == 'call_followup' and (row.payload or {}).get('activity_id') and body.status == 'completed':
                raise WorkConflict('Source-bound note completion requires canonical note-plan')
            return finish_work(
                session,
                principal.workspace_id,
                work_id,
                body.lease_token,
                result=body.result.model_dump(exclude_none=True),
                status=body.status,
            )
    except (WorkConflict, ValueError):
        raise HTTPException(status_code=409, detail="Work conflict") from None


@router.post("/api/v1/agent/work/{work_id}/resolve")
def resolve_review(
    work_id: UUID, body: ResolveReviewBody, principal: Principal, session: Database
):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            return resolve_waiting_review(
                session,
                principal.workspace_id,
                work_id,
                actor_id=principal.actor_id,
                resolution_id=body.resolution_id,
                expected_result_hash=body.expected_result_hash,
                result=body.result.model_dump(),
            )
    except (WorkConflict, ValueError, IntegrityError):
        raise HTTPException(
            status_code=409, detail="Review resolution conflict"
        ) from None


@router.post("/api/v1/agent/work/{work_id}/fail")
def fail(work_id: UUID, body: FailBody, principal: Principal, session: Database):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            return fail_work(
                session, principal.workspace_id, work_id, **body.model_dump()
            )
    except WorkConflict:
        raise HTTPException(status_code=409, detail="Work conflict") from None


@router.post("/api/v1/agent/work/{work_id}/execute")
def execute(work_id: UUID, body: LeaseBody, principal: Principal, session: Database):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            return execute_callback(
                session, principal.workspace_id, work_id, body.lease_token
            )
    except WorkConflict:
        raise HTTPException(status_code=409, detail="Work conflict") from None
    except Exception:
        raise HTTPException(
            status_code=502, detail="Provider action not confirmed"
        ) from None


@router.post("/api/v1/agent/work/{work_id}/next-action")
def next_action(
    work_id: UUID, body: NextActionBody, principal: Principal, session: Database
):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            row = locked_work(session, principal.workspace_id, work_id)
            if row.kind == 'call_followup' and (row.payload or {}).get('activity_id'):
                raise WorkConflict('Source-bound notes require canonical note-plan')
            task_id = uuid5(work_id, "next-action")
            result = {
                "summary": body.title,
                "evidence": row.payload.get("evidence", []),
                "next_action": {
                    "task_id": str(task_id),
                    "owner": "head-of-sales",
                    "due_at": body.due_at.isoformat(),
                },
            }
            if row.status == "completed" and row.last_lease_token == body.lease_token:
                return finish_work(
                    session,
                    principal.workspace_id,
                    work_id,
                    body.lease_token,
                    result=result,
                )
            validate_lease(row, body.lease_token)
            lead = session.scalar(
                select(Lead)
                .where(
                    Lead.workspace_id == principal.workspace_id, Lead.id == row.lead_id
                )
                .with_for_update()
            )
            if (
                not lead
                or lead_is_suppressed(session, lead)
                or lead.version != body.expected_lead_version
                or body.due_at <= datetime.now(UTC)
            ):
                raise WorkConflict("Lead changed or invalid date")
            if body.supersede_agent_followups:
                tasks = session.scalars(
                    select(Task)
                    .where(
                        Task.workspace_id == principal.workspace_id,
                        Task.lead_id == lead.id,
                        Task.status == "open",
                        Task.task_type.in_(("email", "follow_up")),
                        Task.source_rule == "head_of_sales",
                    )
                    .with_for_update()
                )
                for task in tasks:
                    task.status = "cancelled"
            task = Task(
                id=task_id,
                workspace_id=principal.workspace_id,
                lead_id=lead.id,
                account_id=lead.account_id,
                task_type=body.task_type,
                title=body.title,
                due_at=body.due_at,
                owner_user_id=principal.actor_id,
                status="open",
                source_rule="head_of_sales",
            )
            session.add(task)
            lead.updated_at = datetime.now(UTC)
            session.add(
                Activity(
                    id=uuid5(work_id, "next-action-activity"),
                    workspace_id=principal.workspace_id,
                    account_id=lead.account_id,
                    lead_id=lead.id,
                    activity_type="task",
                    title=body.title,
                    occurred_at=datetime.now(UTC),
                    source_system="agent",
                    actor_type="agent",
                    actor_id=principal.actor_id,
                )
            )
            session.add(
                AuditEvent(
                    id=uuid5(work_id, "next-action-audit"),
                    workspace_id=principal.workspace_id,
                    command_id=uuid5(work_id, "next-action"),
                    actor_id=principal.actor_id,
                    action="agent.next_action_scheduled",
                    entity_type="lead",
                    entity_id=lead.id,
                    details={
                        "task_id": str(task_id),
                        "work_id": str(work_id),
                        "supersede_agent_followups": body.supersede_agent_followups,
                    },
                )
            )
            session.flush()
            enqueue_task_work(session, task)
            return finish_work(
                session,
                principal.workspace_id,
                work_id,
                body.lease_token,
                result=result,
            )
    except (WorkConflict, ValueError):
        raise HTTPException(status_code=409, detail="Work conflict") from None


class NoteFact(StrictBody):
    field: Literal['answer_kind','useful','decision_maker','interlocutor_role','repeat_reason']
    value: Any
    quote: str = Field(min_length=1, max_length=2000)


class DraftContent(StrictBody):
    recipient: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=240)
    body_html: str = Field(min_length=1, max_length=20000)


class NoteAction(StrictBody):
    key: str = Field(pattern='^[a-z0-9_-]{1,64}$')
    task_type: Literal['call','email','follow_up']
    title: str = Field(min_length=1, max_length=512)
    due_at: AwareDatetime
    quote: str = Field(min_length=1, max_length=2000)
    existing_task_id: UUID | None = None
    draft: DraftContent | None = None
    expected_task_version: int | None = Field(default=None, ge=1)


class NotePlanBody(LeaseBody):
    expected_lead_version: int = Field(ge=1)
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    facts: list[NoteFact] = Field(default_factory=list, max_length=5)
    actions: list[NoteAction] = Field(default_factory=list, max_length=5)
    summary: str = Field(min_length=1, max_length=2000)


@router.post('/api/v1/agent/work/{work_id}/note-plan')
def note_plan(work_id: UUID, body: NotePlanBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from src.crm.services.note_plan_service import apply_note_plan
    try:
        with session.begin():
            return apply_note_plan(session, principal, work_id, body)
    except (WorkConflict, ValueError, TypeError):
        raise HTTPException(status_code=409, detail='Work conflict') from None


@router.get('/api/v1/agent/notes/audit')
def audit_notes(principal: Principal, session: Database, cursor: UUID | None = None,
                cutoff: AwareDatetime | None = None, limit: int = 20):
    require_scope(principal, 'work:read')
    cutoff=cutoff or datetime.now(UTC)
    limit=max(1,min(limit,50))
    query=select(AgentWork).where(AgentWork.workspace_id==principal.workspace_id,
        AgentWork.kind=='call_followup',AgentWork.created_at<=cutoff)
    if cursor:query=query.where(AgentWork.id>cursor)
    rows=list(session.scalars(query.order_by(AgentWork.id).limit(limit+1)))
    return {'items':[with_note_processing(session,principal.workspace_id,serialize_work(row),verify_drafts=True) for row in rows[:limit]],
        'next_cursor':str(rows[limit-1].id) if len(rows)>limit else None,
        'cutoff':cutoff.astimezone(UTC).isoformat(),
        'scope':'CRM obligations, recorded Calendar receipts, fresh read-only draft MIME; never proof of sending'}


class ReconcileNotesBody(StrictBody):
    limit: int = Field(default=50, ge=1, le=100)


@router.post('/api/v1/agent/notes/reconcile')
def reconcile_notes(body: ReconcileNotesBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from src.crm.services.note_source_service import reconcile_note_sources
    with session.begin():
        return reconcile_note_sources(session, principal.workspace_id, body.limit)


class ObservationsBody(StrictBody):
    observations: list[dict[str, Any]] = Field(max_length=20)


@router.post("/api/v1/agent/providers/observations")
def observations(body: ObservationsBody, principal: Principal, session: Database):
    require_scope(principal, "providers:sync")
    import json
    from src.crm.services.google_observation_service import (
        apply_observation,
        ObservationError,
    )

    if len(json.dumps(body.model_dump()).encode()) > 256000:
        raise HTTPException(status_code=422, detail="Observation batch too large")
    if any(
        item.get("source_scope") not in principal.source_scopes
        for item in body.observations
    ):
        raise HTTPException(status_code=403, detail="Provider source not authorized")
    try:
        with session.begin():
            items = [
                apply_observation(session, principal.workspace_id, observation)
                for observation in body.observations
            ]
        return {
            "accepted": sum(not i["duplicate"] for i in items),
            "duplicates": sum(i["duplicate"] for i in items),
            "items": items,
        }
    except (ObservationError, ValueError, KeyError):
        raise HTTPException(
            status_code=422, detail="Invalid provider observation"
        ) from None


class GoogleSyncBody(StrictBody):
    limit: int = Field(default=20, ge=1, le=20)
    cursor: str | None = Field(default=None, max_length=2000)
    since: AwareDatetime | None = None


@router.post("/api/v1/agent/providers/google/sync")
def google_sync(body: GoogleSyncBody, principal: Principal, session: Database):
    require_scope(principal, "providers:sync")
    from src.crm.connectors.google_transport import GmailTransport

    mailbox = os.environ.get("CRM_GOOGLE_MAILBOX", "")
    credentials = os.environ.get("GOOGLE_GMAIL_CREDENTIALS_FILE", "")
    if not mailbox or not credentials:
        return {
            "configured": False,
            "accepted": 0,
            "duplicates": 0,
            "items": [],
            "reason": "Use local Google collector",
        }
    try:
        page = GmailTransport(
            credentials_file=credentials,
            mailbox_email=mailbox,
            since=body.since,
            limit=body.limit,
        ).fetch("mailbox:" + mailbox, body.cursor)
    except Exception:
        raise HTTPException(status_code=502, detail="Google read failed") from None
    result = observations(
        ObservationsBody(observations=page["observations"]), principal, session
    )
    return result | {
        "configured": True,
        "next_cursor": page["next_cursor"],
        "scanned_since": page["scanned_since"],
    }
