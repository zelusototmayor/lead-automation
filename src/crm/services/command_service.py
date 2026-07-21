"""Authorized human commands with atomic outbox and append-only audit writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
from src.crm.persistence.models import Activity, AuditEvent
from src.crm.services.account_service import (
    IdentityHints,
    IdentityReviewRequired,
    normalize_company_name,
)


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


def _account_lifecycle(target: str) -> str:
    if target == "won":
        return "customer"
    rank = highest_stage_rank(0, target)
    if rank >= 60:
        return "proposal"
    if rank >= 40:
        return "meeting"
    return "potential"


def _ensure_account_for_transition(uow, lead, target: str):
    lead_city = lead.city
    account = (
        uow.accounts.get(lead.workspace_id, lead.account_id, for_update=True)
        if lead.account_id is not None
        else None
    )
    contact = (
        uow.contacts.get(lead.workspace_id, lead.contact_id, for_update=True)
        if lead.contact_id is not None
        else None
    )
    if lead.account_id is not None and account is None:
        raise _conflict() from None
    if lead.contact_id is not None and (
        contact is None or account is None or contact.account_id != account.id
    ):
        raise _conflict() from None
    hints = None
    if account is None or contact is None:
        if not lead.contact_email or (account is None and not lead.company_name):
            raise _conflict() from None
        email = str(lead.contact_email)
        hints = IdentityHints(
            company_name=lead.company_name,
            contact_name=lead.contact_name,
            contact_email=email,
            sector=lead.sector,
            vertical=lead.commercial_vertical,
            source_origin=lead.source_origin,
        )
        uow.lock_identities(lead.workspace_id, (f"email:{email.casefold()}",))
    if account is None:
        assert hints is not None
        candidates = {
            candidate.id: candidate
            for candidate in uow.account_candidates(lead.workspace_id, hints)
        }
        if len(candidates) > 1:
            raise _conflict() from None
        account = next(iter(candidates.values()), None)
        if account is None:
            account = uow.new_account(lead.workspace_id, hints)
        elif account.normalized_name != normalize_company_name(lead.company_name):
            raise _conflict() from None
        lead.account_id = account.id
    elif (
        lead.company_name is not None
        and account.normalized_name != normalize_company_name(lead.company_name)
    ):
        raise _conflict() from None
    if contact is None:
        assert hints is not None
        contact = uow.new_contact(lead.workspace_id, account.id, hints)
        if contact.phone is None:
            contact.phone = lead.contact_phone
        elif lead.contact_phone is not None and contact.phone != lead.contact_phone:
            raise _conflict() from None
        lead.contact_id = contact.id
    account.highest_stage_rank = max(
        account.highest_stage_rank, highest_stage_rank(lead.highest_stage_rank, target)
    )
    desired_lifecycle = _account_lifecycle(target)
    lifecycle_order = {"potential": 0, "meeting": 1, "proposal": 2, "customer": 3}
    if lifecycle_order.get(desired_lifecycle, 0) > lifecycle_order.get(
        account.lifecycle_stage, 0
    ):
        account.lifecycle_stage = desired_lifecycle
    if account.city is None:
        account.city = lead_city
    elif lead_city is not None and account.city != lead_city:
        raise _conflict() from None
    if lead.account_id == account.id:
        lead.company_name = None
        lead.contact_name = None
        lead.contact_email = None
        lead.contact_phone = None
        lead.city = None
    return account


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
            account_required = requires_account(
                target,
                lead.highest_stage_rank,
                lead.account_id is not None,
            )
            if account_required or lead.account_id is not None:
                _ensure_account_for_transition(self.uow, lead, target)
            previous = lead.stage
            lead.stage = target
            lead.highest_stage_rank = highest_stage_rank(
                lead.highest_stage_rank, target
            )
            self.uow.session.flush()
        except (IdentityReviewRequired, TypeError, ValueError):
            raise _conflict() from None

        audit_id = uuid5(
            command.workspace_id,
            f"{command.command_id}:audit:lead.stage-transitioned",
        )
        self.uow.activities.add(
            Activity(
                id=uuid5(
                    command.workspace_id,
                    f"{command.command_id}:activity:lead.stage-transitioned",
                ),
                workspace_id=command.workspace_id,
                account_id=lead.account_id,
                lead_id=lead.id,
                activity_type="stage_change",
                occurred_at=datetime.now(UTC),
                title="Stage changed",
                summary=None,
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
