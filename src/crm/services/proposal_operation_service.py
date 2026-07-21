"""Canonical proposal pipeline commands in caller-owned transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from uuid import UUID, uuid5

from src.crm.ingestion.outbox import enqueue_outbox_event
from src.crm.persistence.models import (
    PROPOSAL_SENT_OR_LATER_STATUSES,
    PROPOSAL_STATUSES,
    Activity,
    AuditEvent,
)
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    HumanCommandPrincipal,
)


@dataclass(frozen=True, slots=True)
class UpdateProposalPipelineCommand:
    command_id: UUID
    workspace_id: UUID
    proposal_id: UUID
    expected_version: int
    status: str
    probability: Decimal | None
    forecast_category: str | None
    next_action: str | None
    next_action_due_at: datetime | None
    lost_reason: str | None


@dataclass(frozen=True, slots=True)
class ProposalOperationResult:
    command_id: UUID
    aggregate_id: UUID
    version: int
    replayed: bool


def _conflict() -> CommandConflictError:
    return CommandConflictError("command conflict")


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or value != value.strip()
        or not value
        or len(value) > maximum
    ):
        raise _conflict() from None
    return value


def _semantic_hash(command: UpdateProposalPipelineCommand) -> str:
    canonical = json.dumps(
        {
            "action": "update-pipeline",
            "expected_version": command.expected_version,
            "forecast_category": command.forecast_category,
            "lost_reason": command.lost_reason,
            "next_action": command.next_action,
            "next_action_due_at": command.next_action_due_at.astimezone(UTC).isoformat()
            if command.next_action_due_at is not None
            else None,
            "probability": str(command.probability.quantize(Decimal("0.01")))
            if command.probability is not None
            else None,
            "proposal_id": str(command.proposal_id),
            "status": command.status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ProposalOperationService:
    """Apply explicit proposal pipeline state; never publish or send outbound."""

    def __init__(self, uow):
        self.uow = uow

    def update_pipeline(
        self,
        principal: HumanCommandPrincipal,
        command: UpdateProposalPipelineCommand,
    ) -> ProposalOperationResult:
        self._authorize(principal, command)
        if (
            type(command) is not UpdateProposalPipelineCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.proposal_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
            or command.status not in PROPOSAL_STATUSES
            or command.status == "won"
        ):
            raise _conflict() from None
        probability = self._probability(command.probability)
        forecast_category = _optional_text(command.forecast_category, maximum=64)
        next_action = _optional_text(command.next_action, maximum=2048)
        lost_reason = _optional_text(command.lost_reason, maximum=2048)
        due_at = command.next_action_due_at
        if due_at is not None:
            if (
                type(due_at) is not datetime
                or due_at.tzinfo is None
                or due_at.utcoffset() is None
                or next_action is None
            ):
                raise _conflict() from None
            due_at = due_at.astimezone(UTC)
        if (command.status == "lost") != (lost_reason is not None):
            raise _conflict() from None

        semantic_hash = _semantic_hash(command)
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        proposal = self.uow.proposals.get(
            command.workspace_id, command.proposal_id, for_update=True
        )
        if proposal is None or proposal.version != command.expected_version:
            raise _conflict() from None
        if command.status in PROPOSAL_SENT_OR_LATER_STATUSES:
            if proposal.sent_at is None or proposal.sent_verification_state is None:
                raise _conflict() from None
        elif proposal.sent_at is not None:
            raise _conflict() from None

        now = datetime.now(UTC)
        proposal.status = command.status
        proposal.probability = probability
        proposal.probability_source = "manual" if probability is not None else None
        proposal.forecast_category = forecast_category
        proposal.next_action = next_action
        proposal.next_action_due_at = due_at
        if command.status == "won":
            proposal.won_at = proposal.won_at or now
            proposal.lost_at = None
            proposal.lost_reason = None
        elif command.status == "lost":
            proposal.won_at = None
            proposal.lost_at = proposal.lost_at or now
            proposal.lost_reason = lost_reason
        else:
            proposal.won_at = None
            proposal.lost_at = None
            proposal.lost_reason = None
        proposal.updated_at = now
        self.uow.session.flush()

        version = proposal.version
        event_type = "proposal.pipeline_updated"
        event_payload = {
            "proposal_id": str(proposal.id),
            "status": proposal.status,
            "version": version,
        }
        self.uow.activities.add(
            Activity(
                id=uuid5(
                    command.workspace_id,
                    f"{command.command_id}:activity:{event_type}",
                ),
                workspace_id=command.workspace_id,
                account_id=proposal.account_id,
                activity_type="note",
                occurred_at=now,
                title="Proposal pipeline updated",
                semantic_fingerprint=semantic_hash,
                source_system="manual",
                actor_type="human",
                actor_id=principal.actor_id,
            )
        )
        enqueue_outbox_event(
            self.uow,
            workspace_id=command.workspace_id,
            command_id=command.command_id,
            semantic_hash=semantic_hash,
            event_type=event_type,
            aggregate_type="proposal",
            aggregate_id=proposal.id,
            payload=event_payload,
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
                entity_type="proposal",
                entity_id=proposal.id,
                details={"status": proposal.status, "version": version},
            )
        )
        return ProposalOperationResult(command.command_id, proposal.id, version, False)

    @staticmethod
    def _probability(value: object) -> Decimal | None:
        if value is None:
            return None
        if (
            type(value) is not Decimal
            or not value.is_finite()
            or not Decimal("0") <= value <= Decimal("100")
        ):
            raise _conflict() from None
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -2:
            raise _conflict() from None
        return value.quantize(Decimal("0.01"))

    @staticmethod
    def _authorize(principal, command) -> None:
        if (
            type(principal) is not HumanCommandPrincipal
            or type(principal.actor_id) is not UUID
            or type(principal.workspace_id) is not UUID
            or type(principal.permissions) is not frozenset
            or principal.workspace_id != getattr(command, "workspace_id", None)
            or "crm:proposal:write" not in principal.permissions
        ):
            raise CommandAuthorizationError("command forbidden") from None

    def _claim_or_replay(
        self, command: UpdateProposalPipelineCommand, semantic_hash: str
    ) -> ProposalOperationResult | None:
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
            or replay.aggregate_id != command.proposal_id
        ):
            raise _conflict() from None
        return ProposalOperationResult(
            command.command_id,
            replay.aggregate_id,
            int(replay.payload["version"]),
            True,
        )
