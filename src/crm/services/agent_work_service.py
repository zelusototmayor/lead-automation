"""Lease-fenced execution, bounded retries and duplicate-safe terminal results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4, uuid5

from sqlalchemy import select, true
from sqlalchemy.dialects.postgresql import insert
from src.crm.persistence.models import AgentWork, Task, Lead, Contact, SourceIdentity


class WorkConflict(ValueError):
    pass


def lead_is_suppressed(session, lead):
    if lead.stage in {"lost", "not_a_fit", "won"}:
        return True
    if lead.contact_id:
        contact = session.scalar(
            select(Contact).where(
                Contact.workspace_id == lead.workspace_id, Contact.id == lead.contact_id
            )
        )
        if contact and contact.status == "inactive":
            return True
    if lead.source_identity_id:
        source = session.scalar(
            select(SourceIdentity).where(
                SourceIdentity.workspace_id == lead.workspace_id,
                SourceIdentity.id == lead.source_identity_id,
            )
        )
        if source and (source.metadata_json or {}).get("suppressed") is True:
            return True
    return False


def enqueue_work(
    session,
    *,
    workspace_id,
    source_key,
    kind,
    payload=None,
    task_id=None,
    lead_id=None,
    available_at=None,
):
    work_id = uuid5(workspace_id, f"agent-work:{source_key}")
    session.execute(
        insert(AgentWork)
        .values(
            id=work_id,
            workspace_id=workspace_id,
            source_key=source_key,
            kind=kind,
            payload=payload or {},
            task_id=task_id,
            lead_id=lead_id,
            available_at=available_at or datetime.now(UTC),
        )
        .on_conflict_do_nothing(constraint="uq_agent_work_source")
    )
    return work_id


def enqueue_callback(session, task):
    if task.task_type != "call":
        return None
    session.flush()
    return enqueue_work(
        session,
        workspace_id=task.workspace_id,
        source_key=f"callback:{task.id}:v{task.version}",
        kind="calendar_callback",
        task_id=task.id,
        lead_id=task.lead_id,
        payload={"task_version": task.version},
    )


def enqueue_task_work(session, task):
    if task.task_type == "call":
        return enqueue_callback(session, task)
    if task.task_type not in {"email", "follow_up"} or task.status != "open":
        return None
    session.flush()
    return enqueue_work(
        session,
        workspace_id=task.workspace_id,
        source_key=f"followup:{task.id}:v{task.version}",
        kind="followup_due",
        task_id=task.id,
        lead_id=task.lead_id,
        available_at=task.due_at,
        payload={"task_version": task.version, "title": task.title},
    )


def serialize_work(row, *, include_lease=False):
    value = {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "task_id": str(row.task_id) if row.task_id else None,
        "lead_id": str(row.lead_id) if row.lead_id else None,
        "payload": row.payload,
        "attempt": row.attempts,
        "available_at": row.available_at.isoformat(),
        "result": row.result,
        "error": row.error,
        "updated_at": row.updated_at.isoformat(),
    }
    if include_lease:
        value.update(
            lease_token=str(row.lease_token), lease_until=row.lease_until.isoformat()
        )
    return value


def claim_work(
    session,
    workspace_id,
    *,
    worker_id,
    limit=20,
    lease_seconds=300,
    now=None,
    kinds=None,
):
    now = now or datetime.now(UTC)
    if not (
        1 <= limit <= 20 and 30 <= lease_seconds <= 600 and 1 <= len(worker_id) <= 128
    ):
        raise WorkConflict("invalid claim")
    expired = list(
        session.scalars(
            select(AgentWork)
            .where(
                AgentWork.workspace_id == workspace_id,
                AgentWork.status == "running",
                AgentWork.lease_until <= now,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for row in expired:
        row.status = "failed" if row.attempts >= 3 else "queued"
        row.error = "Worker lease expired"
        row.last_lease_token = row.lease_token
        row.lease_token = row.lease_until = None
    session.flush()
    rows = list(
        session.scalars(
            select(AgentWork)
            .where(
                AgentWork.workspace_id == workspace_id,
                AgentWork.status == "queued",
                AgentWork.attempts < 3,
                AgentWork.available_at <= now,
                AgentWork.kind.in_(kinds) if kinds else true(),
            )
            .order_by(AgentWork.available_at, AgentWork.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    claimed = []
    for row in rows:
        if row.kind == "followup_due" and row.task_id:
            task = session.scalar(
                select(Task).where(
                    Task.workspace_id == workspace_id, Task.id == row.task_id
                )
            )
            lead = (
                session.scalar(
                    select(Lead).where(
                        Lead.workspace_id == workspace_id, Lead.id == row.lead_id
                    )
                )
                if row.lead_id
                else None
            )
            if (
                (lead is not None and lead_is_suppressed(session, lead))
                or task is None
                or task.status != "open"
                or task.version != row.payload.get("task_version")
            ):
                row.status = "completed"
                row.result = {
                    "summary": "Ação substituída ou concluída; sem trabalho adicional",
                    "evidence": [],
                }
                continue
        claimed.append(row)
        row.status, row.worker_id = "running", worker_id
        row.attempts += 1
        row.lease_token, row.lease_until = (
            uuid4(),
            now + timedelta(seconds=lease_seconds),
        )
        row.updated_at = now
    session.flush()
    return [serialize_work(row, include_lease=True) for row in claimed]


def locked_work(session, workspace_id, work_id):
    row = session.scalar(
        select(AgentWork)
        .where(AgentWork.workspace_id == workspace_id, AgentWork.id == work_id)
        .with_for_update()
    )
    if row is None:
        raise WorkConflict("work unavailable")
    return row


def validate_lease(row, lease_token, now=None):
    if (
        row.status != "running"
        or row.lease_token != lease_token
        or row.lease_until <= (now or datetime.now(UTC))
    ):
        raise WorkConflict("work lease conflict")


def finish_work(
    session, workspace_id, work_id, lease_token, *, result, status="completed", now=None
):
    if status not in {"completed", "waiting"}:
        raise WorkConflict("invalid work outcome")
    encoded = json.dumps(
        {"status": status, "result": result},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode()) > 16000:
        raise WorkConflict("result too large")
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    row = locked_work(session, workspace_id, work_id)
    if (
        row.status == status
        and row.last_lease_token == lease_token
        and row.result_hash == fingerprint
    ):
        return serialize_work(row) | {"replayed": True}
    validate_lease(row, lease_token, now)
    row.status, row.result, row.result_hash = status, result, fingerprint
    row.last_lease_token = row.lease_token
    row.lease_token = row.lease_until = None
    row.error = None
    session.flush()
    return serialize_work(row) | {"replayed": False}


def fail_work(
    session,
    workspace_id,
    work_id,
    lease_token,
    *,
    retryable=True,
    reason="Execution failed",
    now=None,
):
    now = now or datetime.now(UTC)
    row = locked_work(session, workspace_id, work_id)
    if row.last_lease_token == lease_token and row.status in {"queued", "failed"}:
        return serialize_work(row) | {"replayed": True}
    validate_lease(row, lease_token, now)
    row.status = "queued" if retryable and row.attempts < 3 else "failed"
    row.available_at = now + timedelta(seconds=30 * 2 ** (row.attempts - 1))
    row.error = reason[:256]
    row.last_lease_token = row.lease_token
    row.lease_token = row.lease_until = None
    session.flush()
    return serialize_work(row) | {"replayed": False}
