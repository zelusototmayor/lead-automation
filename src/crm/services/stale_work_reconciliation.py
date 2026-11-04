"""Audited closure of legacy waiting work against an already completed task.

This records an operator's exact linkage and canonical completion evidence. It
does not execute tasks, infer a send, classify notes, or acquire a worker lease.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid5

from sqlalchemy import select

from src.crm.persistence.models import Activity, AgentWork, AuditEvent, Task
from src.crm.services.agent_work_service import WorkConflict, locked_work, serialize_work
from src.crm.services.note_source_service import source_digest


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def reconcile_completed_task(session, workspace_id, actor_id, work_id, body):
    request = body.model_dump(mode="json")
    request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
    row = locked_work(session, workspace_id, work_id)
    prior = session.scalar(select(AuditEvent).where(
        AuditEvent.workspace_id == workspace_id,
        AuditEvent.command_id == body.resolution_id,
    ))
    if prior is not None:
        if (prior.action != "agent.obligation_reconciled" or prior.entity_id != work_id
                or prior.actor_id != actor_id or prior.details.get("request_hash") != request_hash
                or row.status != "completed" or row.result_hash != prior.details.get("result_hash")):
            raise WorkConflict("Resolution identity was already used")
        return serialize_work(row) | {"resolution_id": str(body.resolution_id), "replayed": True}
    if (row.status != "waiting" or row.result_hash != body.expected_result_hash
            or row.lease_token is not None or row.lease_until is not None
            or row.kind not in {"followup_due", "call_followup"}
            or any(k in row.payload for k in ("followup_v1", "followup_anchor_v1", "activity_id"))):
        raise WorkConflict("Only unchanged legacy waiting obligations can be reconciled")
    task = session.scalar(select(Task).where(
        Task.workspace_id == workspace_id, Task.id == body.task_id,
    ).with_for_update())
    if (task is None or task.lead_id != row.lead_id or task.version != body.expected_task_version
            or task.status != "completed" or task.task_type not in {"email", "follow_up"}
            or not task.completed_at or not task.completion_activity_id
            or task.completed_at < task.created_at):
        raise WorkConflict("Exact obligation is not completed at the expected version")
    completion = session.scalar(select(Activity).where(
        Activity.workspace_id == workspace_id, Activity.id == task.completion_activity_id,
        Activity.lead_id == row.lead_id,
    ))
    if (completion is None or completion.occurred_at != task.completed_at
            or completion.actor_type != "human"
            or not (completion.activity_type == "task"
                or (completion.activity_type == "email_sent"
                    and completion.id == body.evidence_activity_id))):
        raise WorkConflict("Canonical human task completion evidence is unavailable")
    evidence = session.scalar(select(Activity).where(
        Activity.workspace_id == workspace_id, Activity.id == body.evidence_activity_id,
        Activity.lead_id == row.lead_id,
    ))
    # A stale waiting disposition can be rewritten after the business work was
    # fulfilled. Bind chronology to the exact obligation, not that later edit.
    if (evidence is None or evidence.activity_type not in {"note", "email_sent"}
            or not evidence.summary or evidence.actor_type in {"import", "migration", "system"}
            or evidence.occurred_at < task.created_at
            or source_digest(evidence) != body.expected_evidence_digest):
        raise WorkConflict("Current canonical fulfillment evidence does not match")
    if session.scalar(select(Activity.id).where(
        Activity.workspace_id == workspace_id, Activity.supersedes_activity_id == evidence.id,
    ).limit(1)):
        raise WorkConflict("Canonical fulfillment evidence was superseded")
    source = None
    linked_work = None
    if row.kind == "followup_due":
        if row.task_id != task.id or body.source_activity_id or body.expected_source_digest:
            raise WorkConflict("Followup does not belong to the exact completed task")
    else:
        if (row.task_id is not None or row.payload.get("callback_task_id")
                or set(row.payload) - {"summary", "outcome_code", "callback_task_id"}
                or task.source_rule != "manual_next_action"
                or not body.source_activity_id or not body.expected_source_digest):
            raise WorkConflict("Legacy call has independent or unverified obligations")
        sources = list(session.scalars(select(Activity).where(
            Activity.workspace_id == workspace_id, Activity.lead_id == row.lead_id,
            Activity.activity_type == "call", Activity.summary == row.payload.get("summary"),
        )))
        if len(sources) != 1:
            raise WorkConflict("Legacy call source is ambiguous")
        source = sources[0]
        superseded = session.scalar(select(Activity.id).where(
            Activity.workspace_id == workspace_id, Activity.supersedes_activity_id == source.id,
        ).limit(1))
        if (not source.summary or source.id != body.source_activity_id or source_digest(source) != body.expected_source_digest
                or source.outcome_code != row.payload.get("outcome_code") or superseded
                or source.actor_type in {"import", "migration", "system"}
                or not source.occurred_at <= row.created_at <= task.created_at):
            raise WorkConflict("Call source or obligation chronology changed")
        # The operator attests the legacy call-to-task relation; old records have
        # no source-to-task FK. Verify its exact canonical task work and chronology,
        # without claiming that later follow-ups or other call obligations ran.
        linked_work = session.scalar(select(AgentWork).where(
            AgentWork.workspace_id == workspace_id, AgentWork.lead_id == row.lead_id,
            AgentWork.kind == "followup_due", AgentWork.task_id == task.id,
            AgentWork.source_key.startswith(f"followup:{task.id}:"),
        ).order_by(AgentWork.created_at, AgentWork.id).limit(1))
        if linked_work is None:
            raise WorkConflict("Completed obligation lacks its canonical work linkage")
    result = {
        "summary": "Legacy waiting obligation reconciled to an already completed canonical task; no new execution or send.",
        "resolution": "fulfilled_by_existing_task",
        "operator_reason": body.reason,
        "evidence": [
            {"task_id": str(task.id), "version": task.version, "status": "completed",
             "completion_activity_id": str(completion.id), "completed_at": task.completed_at.isoformat()},
            {"activity_id": str(evidence.id), "source_digest": source_digest(evidence)},
        ],
        "effects_performed": [],
    }
    if source:
        result["evidence"].append({"activity_id": str(source.id), "source_digest": source_digest(source),
                                   "linked_work_id": str(linked_work.id)})
    fingerprint = hashlib.sha256(_json({"status": "completed", "result": result}).encode()).hexdigest()
    history = _json({"previous_result": row.result, "resolution_result": result}).encode()
    encoded = base64.b64encode(history).decode("ascii")
    parts = [encoded[i:i + 2800] for i in range(0, len(encoded), 2800)]
    for index, part in enumerate(parts):
        session.add(AuditEvent(
            id=uuid5(body.resolution_id, f"obligation-evidence:{workspace_id}:{index}"),
            workspace_id=workspace_id,
            command_id=uuid5(body.resolution_id, f"obligation-evidence-command:{index}"),
            actor_id=actor_id, action="agent.obligation_resolution_evidence",
            entity_type="agent_work", entity_id=work_id,
            details={"resolution_id": str(body.resolution_id), "part": index,
                     "parts": len(parts), "history_json_base64": part},
        ))
    session.add(AuditEvent(
        id=uuid5(body.resolution_id, f"obligation-resolution:{workspace_id}"),
        workspace_id=workspace_id, command_id=body.resolution_id, actor_id=actor_id,
        action="agent.obligation_reconciled", entity_type="agent_work", entity_id=work_id,
        details={"request_hash": request_hash, "request": request,
                 "previous_result_hash": row.result_hash, "result_hash": fingerprint,
                 "history_encoding": "base64-json", "history_parts": len(parts),
                 "history_sha256": hashlib.sha256(history).hexdigest(),
                 "task_id": str(task.id), "task_version": task.version},
    ))
    row.status, row.result, row.result_hash = "completed", result, fingerprint
    row.error, row.updated_at = None, datetime.now(UTC)
    session.flush()
    return serialize_work(row) | {"resolution_id": str(body.resolution_id), "replayed": False}
