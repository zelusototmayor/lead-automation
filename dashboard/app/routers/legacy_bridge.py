"""Canonical read adapter for existing Hermes senders; one narrow sent receipt command."""

from datetime import datetime, timezone
import hashlib, json
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from dashboard.app.routers.agent_work import Principal, Database, require_scope
from dashboard.app.db import create_database_engine, create_session_factory
from src.crm.persistence.models import (
    Lead,
    Account,
    Contact,
    Task,
    SourceIdentity,
    Activity,
    IngestEvent,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.command_service import HumanCommandPrincipal
from src.crm.services.lead_operation_service import (
    LeadOperationService,
    LogEmailCommand,
)

router = APIRouter(prefix="/api/v1/agent/legacy")


def rows(session, workspace):
    result = []
    source = session.execute(
        select(Lead, Account, Contact, SourceIdentity)
        .outerjoin(Account, Lead.account_id == Account.id)
        .outerjoin(Contact, Lead.contact_id == Contact.id)
        .outerjoin(SourceIdentity, Lead.source_identity_id == SourceIdentity.id)
        .where(Lead.workspace_id == workspace)
    ).all()
    activities = {}
    for activity in session.scalars(
        select(Activity)
        .where(Activity.workspace_id == workspace)
        .order_by(Activity.occurred_at.desc())
    ):
        activities.setdefault(activity.lead_id, [])
        if len(activities[activity.lead_id]) < 12 and activity.summary:
            activities[activity.lead_id].append(activity.summary)
    tasks = {}
    for task in session.scalars(
        select(Task)
        .where(Task.workspace_id == workspace, Task.status == "open")
        .order_by(Task.due_at)
    ):
        tasks.setdefault(task.lead_id, []).append(task)
    for lead, account, contact, identity in source:
        meta = identity.metadata_json if identity else {}
        raw = meta.get("legacy_row", {})
        item = {k.lower().replace(" ", "_"): v for k, v in raw.items()}
        stage = {
            "not_a_fit": "Not a Fit",
            "meeting_booked": "Meeting Booked",
            "proposal_sent": "Proposal Sent",
        }.get(lead.stage, lead.stage.title())
        pending = tasks.get(lead.id, [])
        initial = next(
            (
                t
                for t in pending
                if t.task_type == "email"
                and t.source_rule
                in ("release:legacy_initial_email", "human:next_action")
            ),
            None,
        )
        if initial and lead.stage not in ("won", "lost", "not_a_fit"):
            stage = "Send Email"
        item.update(
            id=str(lead.id),
            lead_id=str(lead.id),
            row_number=meta.get("row_number") or meta.get("locator"),
            company=account.display_name if account else lead.company_name,
            contact=contact.full_name if contact else lead.contact_name,
            email=str(contact.primary_email or "")
            if contact
            else str(lead.contact_email or ""),
            phone=contact.phone if contact else lead.contact_phone,
            stage=stage,
            priority=lead.priority,
            version=lead.version,
            notes="\n\n".join(activities.get(lead.id, [])),
            _tasks=pending,
            _suppressed=bool(meta.get("suppressed"))
            or lead.stage in ("lost", "not_a_fit", "won")
            or bool(contact and contact.status == "inactive"),
        )
        result.append(item)
    return result


def safe(item):
    return {k: v for k, v in item.items() if not k.startswith("_")}


@router.get("/leads")
def legacy_leads(principal: Principal, session: Database, view: str = "all"):
    require_scope(principal, "legacy:read")
    items = rows(session, principal.workspace_id)
    if view != "all":
        items = [
            i
            for i in items
            if not i["_suppressed"] and any(t.task_type == "call" for t in i["_tasks"])
        ]
    return {
        "leads": [safe(i) for i in items],
        "count": len(items),
        "view": view,
        "source": "postgres",
    }


@router.get("/outreach-followups")
@router.get("/email-followups")
def outreach(principal: Principal, session: Database, view: str = "today"):
    require_scope(principal, "legacy:read")
    today = datetime.now(ZoneInfo("Europe/Lisbon")).date()
    items = []
    for item in rows(session, principal.workspace_id):
        if item["_suppressed"]:
            continue
        for task in item["_tasks"]:
            if task.task_type != "email":
                continue
            due = task.due_at.astimezone(ZoneInfo("Europe/Lisbon")).date()
            if view == "today" and due != today:
                continue
            if view == "due" and due > today:
                continue
            if view == "overdue" and due >= today:
                continue
            if view in ("upcoming", "future") and due <= today:
                continue
            items.append(
                {
                    **safe(item),
                    "task_id": str(task.id),
                    "task": "INITIAL"
                    if task.source_rule == "release:legacy_initial_email"
                    else "REVIEW",
                    "task_label": task.title,
                    "due": due.strftime("%Y/%m/%d"),
                }
            )
    return {"tasks": items, "count": len(items), "view": view, "source": "postgres"}


class SentReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: UUID
    task_id: UUID | None = None
    gmail_message_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{6,128}$")
    gmail_thread_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{6,128}$")
    mailbox: str = Field(min_length=3, max_length=320)
    occurred_at: AwareDatetime
    summary: str = Field(min_length=1, max_length=1000)


@router.post("/sent-receipt")
def sent_receipt(body: SentReceipt, principal: Principal):
    require_scope(principal, "legacy:write")
    if not (
        {body.mailbox, "mailbox:" + body.mailbox} & principal.source_scopes
    ) or body.occurred_at > datetime.now(timezone.utc):
        raise HTTPException(422, "Invalid receipt")
    payload = body.model_dump(mode="json")
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    command_id = uuid5(
        principal.workspace_id,
        "gmail-sent:" + body.mailbox + ":" + body.gmail_message_id,
    )
    engine = create_database_engine()
    try:
        with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
            uow.lock_identities(principal.workspace_id, (f"legacy-sent:{command_id}",))
            previous = uow.session.get(IngestEvent, command_id)
            if previous:
                if previous.payload_hash != fingerprint:
                    raise HTTPException(409, "Receipt conflict")
                return {"success": True, "replayed": True}
            lead = uow.leads.get(principal.workspace_id, body.lead_id, for_update=True)
            if not lead:
                raise HTTPException(404, "Lead unavailable")
            # A real historical send can be recorded even after closure; never reopens it.
            result = LeadOperationService(uow).log_email(
                HumanCommandPrincipal(
                    principal.actor_id,
                    principal.workspace_id,
                    frozenset({"crm:email:log"}),
                ),
                LogEmailCommand(
                    command_id,
                    principal.workspace_id,
                    lead.id,
                    lead.version,
                    "outbound",
                    body.summary
                    + f"\ngmail_message_id={body.gmail_message_id}; gmail_thread_id={body.gmail_thread_id}",
                    body.occurred_at,
                ),
            )
            for pending in uow.session.new:
                if isinstance(pending, Activity):
                    pending.actor_type = "agent"
                    pending.source_system = "agent"
            if body.task_id:
                task = uow.session.scalar(
                    select(Task)
                    .where(
                        Task.workspace_id == principal.workspace_id,
                        Task.id == body.task_id,
                        Task.lead_id == lead.id,
                    )
                    .with_for_update()
                )
                if not task or task.task_type != "email":
                    raise HTTPException(409, "Email task unavailable")
                if task.status == "open":
                    task.status = "completed"
                    task.completed_at = body.occurred_at
                    task.completion_activity_id = uuid5(
                        principal.workspace_id,
                        f"{command_id}:activity:lead.email_logged",
                    )
            uow.session.add(
                IngestEvent(
                    id=command_id,
                    workspace_id=principal.workspace_id,
                    source_system="agent",
                    source_scope=body.mailbox,
                    event_type="sender.gmail-confirmed-receipt",
                    schema_version=1,
                    idempotency_key="sent:"
                    + body.mailbox
                    + ":"
                    + body.gmail_message_id,
                    occurred_at=body.occurred_at,
                    payload=payload,
                    payload_hash=fingerprint,
                    processing_status="applied",
                    applied_at=datetime.now(timezone.utc),
                )
            )
            uow.commit()
            return {"success": True, "replayed": False, "version": result.version}
    finally:
        engine.dispose()
