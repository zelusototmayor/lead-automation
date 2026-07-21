"""Canonical task lifecycle commands in a caller-owned transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from uuid import UUID, uuid5

from src.crm.ingestion.outbox import enqueue_outbox_event
from src.crm.persistence.models import Activity, AuditEvent
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    CommandResult,
    HumanCommandPrincipal,
)


@dataclass(frozen=True, slots=True)
class CompleteTaskCommand:
    command_id: UUID
    workspace_id: UUID
    task_id: UUID
    expected_version: int


@dataclass(frozen=True, slots=True)
class RescheduleTaskCommand:
    command_id: UUID
    workspace_id: UUID
    task_id: UUID
    expected_version: int
    due_at: datetime


@dataclass(frozen=True, slots=True)
class CancelTaskCommand:
    command_id: UUID
    workspace_id: UUID
    task_id: UUID
    expected_version: int


def _conflict() -> CommandConflictError:
    return CommandConflictError("command conflict")


def _semantic_hash(
    command: CompleteTaskCommand | RescheduleTaskCommand | CancelTaskCommand,
) -> str:
    if type(command) is RescheduleTaskCommand:
        action = "reschedule"
        due_at = command.due_at.astimezone(UTC).isoformat()
    elif type(command) is CancelTaskCommand:
        action, due_at = "cancel", None
    else:
        action, due_at = "complete", None
    canonical = json.dumps(
        {
            "action": action,
            "due_at": due_at,
            "expected_version": command.expected_version,
            "task_id": str(command.task_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class TaskCommandService:
    """Mutate tasks with audit/activity/outbox writes; never commit or publish."""

    _PERMISSION = "crm:task:write"

    def __init__(self, uow):
        self.uow = uow

    def complete(
        self, principal: HumanCommandPrincipal, command: CompleteTaskCommand
    ) -> CommandResult:
        self._authorize(principal, command)
        if (
            type(command) is not CompleteTaskCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.task_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
        ):
            raise _conflict() from None

        semantic_hash = _semantic_hash(command)
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        task = self.uow.tasks.get(
            command.workspace_id, command.task_id, for_update=True
        )
        if (
            task is None
            or task.version != command.expected_version
            or task.status != "open"
        ):
            raise _conflict() from None

        completed_at = datetime.now(UTC)
        activity_id = uuid5(
            command.workspace_id,
            f"{command.command_id}:activity:task.completed",
        )
        self.uow.activities.add(
            Activity(
                id=activity_id,
                workspace_id=command.workspace_id,
                account_id=task.account_id,
                lead_id=task.lead_id,
                activity_type="task",
                occurred_at=completed_at,
                title="Task completed",
                semantic_fingerprint=semantic_hash,
                source_system="manual",
                actor_type="human",
                actor_id=principal.actor_id,
            )
        )
        task.status = "completed"
        task.completed_at = completed_at
        task.completion_activity_id = activity_id
        self.uow.session.flush()
        self._record_events(
            principal,
            command,
            semantic_hash,
            action="completed",
            version=task.version,
        )
        return CommandResult(command.command_id, task.id, task.version, False)

    def reschedule(
        self, principal: HumanCommandPrincipal, command: RescheduleTaskCommand
    ) -> CommandResult:
        self._authorize(principal, command)
        if (
            type(command) is not RescheduleTaskCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.task_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
            or type(command.due_at) is not datetime
            or command.due_at.tzinfo is None
            or command.due_at.utcoffset() is None
            or command.due_at <= datetime.now(UTC)
        ):
            raise _conflict() from None
        semantic_hash = _semantic_hash(command)
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        task = self.uow.tasks.get(
            command.workspace_id, command.task_id, for_update=True
        )
        if (
            task is None
            or task.version != command.expected_version
            or task.status != "open"
        ):
            raise _conflict() from None

        task.due_at = command.due_at
        self.uow.activities.add(
            Activity(
                id=uuid5(
                    command.workspace_id,
                    f"{command.command_id}:activity:task.rescheduled",
                ),
                workspace_id=command.workspace_id,
                account_id=task.account_id,
                lead_id=task.lead_id,
                activity_type="task",
                occurred_at=datetime.now(UTC),
                title="Task rescheduled",
                semantic_fingerprint=semantic_hash,
                source_system="manual",
                actor_type="human",
                actor_id=principal.actor_id,
            )
        )
        self.uow.session.flush()
        self._record_events(
            principal,
            command,
            semantic_hash,
            action="rescheduled",
            version=task.version,
            due_at=command.due_at,
        )
        return CommandResult(command.command_id, task.id, task.version, False)

    def cancel(
        self, principal: HumanCommandPrincipal, command: CancelTaskCommand
    ) -> CommandResult:
        self._authorize(principal, command)
        if (
            type(command) is not CancelTaskCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.task_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
        ):
            raise _conflict() from None
        semantic_hash = _semantic_hash(command)
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        task = self.uow.tasks.get(
            command.workspace_id, command.task_id, for_update=True
        )
        if (
            task is None
            or task.version != command.expected_version
            or task.status != "open"
        ):
            raise _conflict() from None

        task.status = "cancelled"
        self.uow.activities.add(
            Activity(
                id=uuid5(
                    command.workspace_id,
                    f"{command.command_id}:activity:task.cancelled",
                ),
                workspace_id=command.workspace_id,
                account_id=task.account_id,
                lead_id=task.lead_id,
                activity_type="task",
                occurred_at=datetime.now(UTC),
                title="Task cancelled",
                semantic_fingerprint=semantic_hash,
                source_system="manual",
                actor_type="human",
                actor_id=principal.actor_id,
            )
        )
        self.uow.session.flush()
        self._record_events(
            principal,
            command,
            semantic_hash,
            action="cancelled",
            version=task.version,
        )
        return CommandResult(command.command_id, task.id, task.version, False)

    def _authorize(self, principal, command) -> None:
        if (
            type(principal) is not HumanCommandPrincipal
            or type(principal.actor_id) is not UUID
            or type(principal.workspace_id) is not UUID
            or type(principal.permissions) is not frozenset
            or principal.workspace_id != getattr(command, "workspace_id", None)
            or self._PERMISSION not in principal.permissions
        ):
            raise CommandAuthorizationError("command forbidden") from None

    def _claim_or_replay(
        self,
        command: CompleteTaskCommand | RescheduleTaskCommand | CancelTaskCommand,
        semantic_hash: str,
    ) -> CommandResult | None:
        self.uow.lock_identities(
            command.workspace_id, (f"human-command:{command.command_id}",)
        )
        replay = self.uow.outbox_events.by_command(
            command.workspace_id, command.command_id
        )
        if replay is None:
            return None
        if (
            replay.semantic_hash != semantic_hash
            or replay.aggregate_id != command.task_id
        ):
            raise _conflict() from None
        return CommandResult(
            command.command_id,
            replay.aggregate_id,
            int(replay.payload["version"]),
            True,
        )

    def _record_events(
        self,
        principal: HumanCommandPrincipal,
        command: CompleteTaskCommand | RescheduleTaskCommand | CancelTaskCommand,
        semantic_hash: str,
        *,
        action: str,
        version: int,
        due_at: datetime | None = None,
    ) -> None:
        event_type = f"task.{action}"
        status = {
            "completed": "completed",
            "rescheduled": "open",
            "cancelled": "cancelled",
        }[action]
        payload = {
            "status": status,
            "task_id": str(command.task_id),
            "version": version,
        }
        if due_at is not None:
            payload["due_at"] = due_at.astimezone(UTC).isoformat()
        enqueue_outbox_event(
            self.uow,
            workspace_id=command.workspace_id,
            command_id=command.command_id,
            semantic_hash=semantic_hash,
            event_type=event_type,
            aggregate_type="task",
            aggregate_id=command.task_id,
            payload=payload,
        )
        self.uow.audit_events.add(
            AuditEvent(
                id=uuid5(
                    command.workspace_id,
                    f"{command.command_id}:audit:{event_type}",
                ),
                workspace_id=command.workspace_id,
                command_id=command.command_id,
                actor_id=principal.actor_id,
                action=event_type,
                entity_type="task",
                entity_id=command.task_id,
                details={
                    key: value for key, value in payload.items() if key != "task_id"
                },
            )
        )
