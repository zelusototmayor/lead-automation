"""Authorized human commands with atomic outbox and append-only audit writes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID, uuid5

from src.crm.domain.stage_policy import (
    highest_stage_rank,
    requires_account,
    resolve_stage,
    validate_transition,
)
from src.crm.ingestion.outbox import enqueue_outbox_event
from src.crm.persistence.models import AuditEvent


class CommandAuthorizationError(RuntimeError):
    """The trusted principal is not authorized for this exact workspace/action."""


class CommandConflictError(RuntimeError):
    """The command cannot be safely applied or replayed."""


@dataclass(frozen=True, slots=True)
class HumanCommandPrincipal:
    actor_id: UUID
    workspace_id: UUID
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class TransitionLeadCommand:
    command_id: UUID
    workspace_id: UUID
    lead_id: UUID
    target_stage: str
    expected_version: int
    reviewed_correction: bool = False


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: UUID
    aggregate_id: UUID
    version: int
    replayed: bool


def _conflict() -> CommandConflictError:
    return CommandConflictError("command conflict")


def _semantic_hash(command: TransitionLeadCommand, target: str) -> str:
    canonical = json.dumps(
        {
            "expected_version": command.expected_version,
            "lead_id": str(command.lead_id),
            "reviewed_correction": command.reviewed_correction,
            "target_stage": target,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class HumanCommandService:
    """Apply commands in the caller-owned transaction; never publish or commit."""

    _PERMISSION = "crm:lead-stage:write"

    def __init__(self, uow):
        self.uow = uow

    def transition_lead(
        self, principal: HumanCommandPrincipal, command: TransitionLeadCommand
    ) -> CommandResult:
        if (
            type(principal) is not HumanCommandPrincipal
            or type(principal.actor_id) is not UUID
            or type(principal.workspace_id) is not UUID
            or type(principal.permissions) is not frozenset
            or principal.workspace_id != getattr(command, "workspace_id", None)
            or self._PERMISSION not in principal.permissions
        ):
            raise CommandAuthorizationError("command forbidden") from None
        if (
            type(command) is not TransitionLeadCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.lead_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
            or type(command.reviewed_correction) is not bool
        ):
            raise _conflict() from None
        try:
            target = resolve_stage(command.target_stage).value
        except (TypeError, ValueError):
            raise _conflict() from None

        self.uow.lock_identities(
            command.workspace_id, (f"human-command:{command.command_id}",)
        )
        semantic_hash = _semantic_hash(command, target)
        replay = self.uow.outbox_events.by_command(
            command.workspace_id, command.command_id
        )
        if replay is not None:
            if (
                replay.semantic_hash != semantic_hash
                or replay.aggregate_id != command.lead_id
            ):
                raise _conflict() from None
            return CommandResult(
                command.command_id,
                replay.aggregate_id,
                int(replay.payload["version"]),
                True,
            )

        lead = self.uow.leads.get(
            command.workspace_id, command.lead_id, for_update=True
        )
        if lead is None or lead.version != command.expected_version:
            raise _conflict() from None
        try:
            validate_transition(lead.stage, target, command.reviewed_correction)
            if (
                requires_account(
                    target,
                    lead.highest_stage_rank,
                    lead.account_id is not None,
                )
                and lead.account_id is None
            ):
                raise _conflict()
            previous = lead.stage
            lead.stage = target
            lead.highest_stage_rank = highest_stage_rank(
                lead.highest_stage_rank, target
            )
            self.uow.session.flush()
        except (TypeError, ValueError):
            raise _conflict() from None

        audit_id = uuid5(
            command.workspace_id,
            f"{command.command_id}:audit:lead.stage-transitioned",
        )
        enqueue_outbox_event(
            self.uow,
            workspace_id=command.workspace_id,
            command_id=command.command_id,
            semantic_hash=semantic_hash,
            event_type="lead.stage_transitioned",
            aggregate_type="lead",
            aggregate_id=lead.id,
            payload={
                "lead_id": str(lead.id),
                "stage": target,
                "version": lead.version,
            },
        )
        self.uow.audit_events.add(
            AuditEvent(
                id=audit_id,
                workspace_id=command.workspace_id,
                command_id=command.command_id,
                actor_id=principal.actor_id,
                action="lead.stage_transitioned",
                entity_type="lead",
                entity_id=lead.id,
                details={
                    "from_stage": previous,
                    "reviewed_correction": command.reviewed_correction,
                    "to_stage": target,
                    "version": lead.version,
                },
            )
        )
        return CommandResult(command.command_id, lead.id, lead.version, False)
