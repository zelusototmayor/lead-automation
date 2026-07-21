"""Canonical operational lead commands in caller-owned transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from uuid import UUID, uuid5

from src.crm.ingestion.outbox import enqueue_outbox_event
from src.crm.persistence.models import Activity, AuditEvent, Task
from src.crm.services.account_service import normalize_company_name
from src.crm.services.command_service import (
    CommandAuthorizationError,
    CommandConflictError,
    HumanCommandPrincipal,
)


@dataclass(frozen=True, slots=True)
class EditLeadCommand:
    command_id: UUID
    workspace_id: UUID
    lead_id: UUID
    expected_version: int
    priority: str
    company_name: str
    contact_name: str
    contact_email: str
    contact_phone: str


@dataclass(frozen=True, slots=True)
class LogCallCommand:
    command_id: UUID
    workspace_id: UUID
    lead_id: UUID
    expected_version: int
    outcome_code: str
    summary: str | None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LogEmailCommand:
    command_id: UUID
    workspace_id: UUID
    lead_id: UUID
    expected_version: int
    direction: str
    summary: str | None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AddNoteCommand:
    command_id: UUID
    workspace_id: UUID
    lead_id: UUID
    expected_version: int
    summary: str


@dataclass(frozen=True, slots=True)
class ScheduleNextActionCommand:
    command_id: UUID
    workspace_id: UUID
    lead_id: UUID
    expected_version: int
    task_type: str
    title: str
    due_at: datetime


@dataclass(frozen=True, slots=True)
class LeadOperationResult:
    command_id: UUID
    aggregate_id: UUID
    version: int
    replayed: bool
    task_id: UUID | None = None
    occurred_at: datetime | None = None


def _conflict() -> CommandConflictError:
    return CommandConflictError("command conflict")


def _bounded_text(value: object, *, maximum: int) -> str:
    if (
        type(value) is not str
        or value != value.strip()
        or not value
        or len(value) > maximum
    ):
        raise _conflict() from None
    return value


def _semantic_hash(action: str, command, fields: dict[str, object]) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "expected_version": command.expected_version,
            "lead_id": str(command.lead_id),
            **fields,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class LeadOperationService:
    """Apply narrow lead operations; never commit, publish, or send outbound."""

    def __init__(self, uow):
        self.uow = uow

    def edit(
        self, principal: HumanCommandPrincipal, command: EditLeadCommand
    ) -> LeadOperationResult:
        self._authorize(principal, command, "crm:lead:edit")
        if (
            type(command) is not EditLeadCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.lead_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
        ):
            raise _conflict() from None
        priority = _bounded_text(command.priority, maximum=64)
        company_name = _bounded_text(command.company_name, maximum=512)
        contact_name = _bounded_text(command.contact_name, maximum=512)
        contact_email = _bounded_text(command.contact_email, maximum=320).lower()
        contact_phone = _bounded_text(command.contact_phone, maximum=64)
        semantic_hash = _semantic_hash(
            "edit",
            command,
            {
                "company_name": company_name,
                "contact_email": contact_email,
                "contact_name": contact_name,
                "contact_phone": contact_phone,
                "priority": priority,
            },
        )
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        lead = self.uow.leads.get(
            command.workspace_id, command.lead_id, for_update=True
        )
        if (
            lead is None
            or lead.version != command.expected_version
            or lead.account_id is None
            or lead.contact_id is None
        ):
            raise _conflict() from None
        account = self.uow.accounts.get(
            command.workspace_id, lead.account_id, for_update=True
        )
        contact = self.uow.contacts.get(
            command.workspace_id, lead.contact_id, for_update=True
        )
        if account is None or contact is None or contact.account_id != account.id:
            raise _conflict() from None

        lead.priority = priority
        account.display_name = company_name
        account.normalized_name = normalize_company_name(company_name)
        contact.full_name = contact_name
        contact.primary_email = contact_email
        contact.phone = contact_phone
        lead.updated_at = datetime.now(UTC)
        self.uow.session.flush()
        self._record(
            principal,
            command,
            semantic_hash,
            event_type="lead.details_updated",
            version=lead.version,
            activity_title="Lead details updated",
            payload={"fields": ["company", "contact", "priority"]},
        )
        return LeadOperationResult(command.command_id, lead.id, lead.version, False)

    def log_call(
        self, principal: HumanCommandPrincipal, command: LogCallCommand
    ) -> LeadOperationResult:
        self._authorize(principal, command, "crm:call:log")
        if (
            type(command) is not LogCallCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.lead_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
            or command.outcome_code
            not in {
                "connected",
                "no_answer",
                "voicemail",
                "wrong_number",
                "not_interested",
                "follow_up",
            }
            or (
                command.summary is not None
                and (
                    type(command.summary) is not str
                    or command.summary != command.summary.strip()
                    or not command.summary
                    or len(command.summary) > 2000
                )
            )
        ):
            raise _conflict() from None
        occurred_at = command.occurred_at or datetime.now(UTC)
        if (
            type(occurred_at) is not datetime
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() is None
            or occurred_at > datetime.now(UTC)
        ):
            raise _conflict() from None
        occurred_at = occurred_at.astimezone(UTC)
        semantic_hash = _semantic_hash(
            "log-call",
            command,
            {
                "occurred_at": command.occurred_at.astimezone(UTC).isoformat()
                if command.occurred_at is not None
                else None,
                "outcome_code": command.outcome_code,
                "summary": command.summary,
            },
        )
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        lead = self.uow.leads.get(
            command.workspace_id, command.lead_id, for_update=True
        )
        if lead is None or lead.version != command.expected_version:
            raise _conflict() from None
        lead.updated_at = datetime.now(UTC)
        self.uow.session.flush()
        self._record(
            principal,
            command,
            semantic_hash,
            event_type="lead.call_logged",
            version=lead.version,
            activity_title="Call logged",
            activity_type="call",
            occurred_at=occurred_at,
            direction="outbound",
            outcome_code=command.outcome_code,
            summary=command.summary,
            payload={
                "occurred_at": occurred_at.isoformat(),
                "outcome_code": command.outcome_code,
            },
        )
        return LeadOperationResult(
            command.command_id,
            lead.id,
            lead.version,
            False,
            occurred_at=occurred_at,
        )

    def log_email(
        self, principal: HumanCommandPrincipal, command: LogEmailCommand
    ) -> LeadOperationResult:
        self._authorize(principal, command, "crm:email:log")
        if (
            type(command) is not LogEmailCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.lead_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
            or command.direction not in {"inbound", "outbound"}
            or (
                command.summary is not None
                and (
                    type(command.summary) is not str
                    or command.summary != command.summary.strip()
                    or not command.summary
                    or len(command.summary) > 2000
                )
            )
        ):
            raise _conflict() from None
        occurred_at = command.occurred_at or datetime.now(UTC)
        if (
            type(occurred_at) is not datetime
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() is None
            or occurred_at > datetime.now(UTC)
        ):
            raise _conflict() from None
        occurred_at = occurred_at.astimezone(UTC)
        semantic_hash = _semantic_hash(
            "log-email",
            command,
            {
                "direction": command.direction,
                "occurred_at": command.occurred_at.astimezone(UTC).isoformat()
                if command.occurred_at is not None
                else None,
                "summary": command.summary,
            },
        )
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        lead = self.uow.leads.get(
            command.workspace_id, command.lead_id, for_update=True
        )
        if lead is None or lead.version != command.expected_version:
            raise _conflict() from None
        lead.updated_at = datetime.now(UTC)
        self.uow.session.flush()
        self._record(
            principal,
            command,
            semantic_hash,
            event_type="lead.email_logged",
            version=lead.version,
            activity_title="Email logged",
            activity_type="email_sent"
            if command.direction == "outbound"
            else "email_received",
            occurred_at=occurred_at,
            direction=command.direction,
            summary=command.summary,
            payload={
                "direction": command.direction,
                "occurred_at": occurred_at.isoformat(),
            },
        )
        return LeadOperationResult(
            command.command_id,
            lead.id,
            lead.version,
            False,
            occurred_at=occurred_at,
        )

    def add_note(
        self, principal: HumanCommandPrincipal, command: AddNoteCommand
    ) -> LeadOperationResult:
        self._authorize(principal, command, "crm:note:write")
        if (
            type(command) is not AddNoteCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.lead_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
        ):
            raise _conflict() from None
        summary = _bounded_text(command.summary, maximum=2000)
        semantic_hash = _semantic_hash("add-note", command, {"summary": summary})
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        lead = self.uow.leads.get(
            command.workspace_id, command.lead_id, for_update=True
        )
        if lead is None or lead.version != command.expected_version:
            raise _conflict() from None
        lead.updated_at = datetime.now(UTC)
        self.uow.session.flush()
        occurred_at = datetime.now(UTC)
        self._record(
            principal,
            command,
            semantic_hash,
            event_type="lead.note_added",
            version=lead.version,
            activity_title="Note added",
            activity_type="note",
            occurred_at=occurred_at,
            summary=summary,
            payload={"occurred_at": occurred_at.isoformat()},
        )
        return LeadOperationResult(
            command.command_id,
            lead.id,
            lead.version,
            False,
            occurred_at=occurred_at,
        )

    def schedule_next_action(
        self,
        principal: HumanCommandPrincipal,
        command: ScheduleNextActionCommand,
    ) -> LeadOperationResult:
        self._authorize(principal, command, "crm:task:write")
        if (
            type(command) is not ScheduleNextActionCommand
            or type(command.command_id) is not UUID
            or type(command.workspace_id) is not UUID
            or type(command.lead_id) is not UUID
            or type(command.expected_version) is not int
            or command.expected_version < 1
            or command.task_type not in {"call", "email", "follow_up"}
            or type(command.due_at) is not datetime
            or command.due_at.tzinfo is None
            or command.due_at.utcoffset() is None
            or command.due_at <= datetime.now(UTC)
        ):
            raise _conflict() from None
        title = _bounded_text(command.title, maximum=512)
        due_at = command.due_at.astimezone(UTC)
        semantic_hash = _semantic_hash(
            "schedule-next-action",
            command,
            {
                "due_at": due_at.isoformat(),
                "task_type": command.task_type,
                "title": title,
            },
        )
        replay = self._claim_or_replay(command, semantic_hash)
        if replay is not None:
            return replay
        lead = self.uow.leads.get(
            command.workspace_id, command.lead_id, for_update=True
        )
        if lead is None or lead.version != command.expected_version:
            raise _conflict() from None
        task_id = uuid5(
            command.workspace_id,
            f"{command.command_id}:task:lead.next_action_scheduled",
        )
        self.uow.tasks.add(
            Task(
                id=task_id,
                workspace_id=command.workspace_id,
                account_id=lead.account_id,
                lead_id=lead.id,
                task_type=command.task_type,
                title=title,
                due_at=due_at,
                owner_user_id=principal.actor_id,
                status="open",
                source_rule="manual_next_action",
            )
        )
        lead.updated_at = datetime.now(UTC)
        self.uow.session.flush()
        self._record(
            principal,
            command,
            semantic_hash,
            event_type="lead.next_action_scheduled",
            version=lead.version,
            activity_title="Next action scheduled",
            activity_type="task",
            payload={"due_at": due_at.isoformat(), "task_type": command.task_type},
            task_id=task_id,
        )
        return LeadOperationResult(
            command.command_id,
            lead.id,
            lead.version,
            False,
            task_id=task_id,
        )

    def _authorize(self, principal, command, permission: str) -> None:
        if (
            type(principal) is not HumanCommandPrincipal
            or type(principal.actor_id) is not UUID
            or type(principal.workspace_id) is not UUID
            or type(principal.permissions) is not frozenset
            or principal.workspace_id != getattr(command, "workspace_id", None)
            or permission not in principal.permissions
        ):
            raise CommandAuthorizationError("command forbidden") from None

    def _claim_or_replay(
        self, command, semantic_hash: str
    ) -> LeadOperationResult | None:
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
            or replay.aggregate_id != command.lead_id
        ):
            raise _conflict() from None
        return LeadOperationResult(
            command.command_id,
            replay.aggregate_id,
            int(replay.payload["version"]),
            True,
            UUID(replay.payload["task_id"]) if replay.payload.get("task_id") else None,
            datetime.fromisoformat(replay.payload["occurred_at"])
            if replay.payload.get("occurred_at")
            else None,
        )

    def _record(
        self,
        principal: HumanCommandPrincipal,
        command,
        semantic_hash: str,
        *,
        event_type: str,
        version: int,
        activity_title: str,
        payload: dict[str, object],
        activity_type: str = "note",
        occurred_at: datetime | None = None,
        direction: str | None = None,
        outcome_code: str | None = None,
        summary: str | None = None,
        task_id: UUID | None = None,
    ) -> None:
        effective_at = occurred_at or datetime.now(UTC)
        self.uow.activities.add(
            Activity(
                id=uuid5(
                    command.workspace_id, f"{command.command_id}:activity:{event_type}"
                ),
                workspace_id=command.workspace_id,
                account_id=self.uow.leads.get(
                    command.workspace_id, command.lead_id
                ).account_id,
                lead_id=command.lead_id,
                activity_type=activity_type,
                occurred_at=effective_at,
                title=activity_title,
                summary=summary,
                direction=direction,
                outcome_code=outcome_code,
                semantic_fingerprint=semantic_hash,
                source_system="manual",
                actor_type="human",
                actor_id=principal.actor_id,
            )
        )
        event_payload = {"lead_id": str(command.lead_id), "version": version, **payload}
        if task_id is not None:
            event_payload["task_id"] = str(task_id)
        enqueue_outbox_event(
            self.uow,
            workspace_id=command.workspace_id,
            command_id=command.command_id,
            semantic_hash=semantic_hash,
            event_type=event_type,
            aggregate_type="lead",
            aggregate_id=command.lead_id,
            payload=event_payload,
        )
        self.uow.audit_events.add(
            AuditEvent(
                id=uuid5(
                    command.workspace_id, f"{command.command_id}:audit:{event_type}"
                ),
                workspace_id=command.workspace_id,
                command_id=command.command_id,
                actor_id=principal.actor_id,
                action=event_type,
                entity_type="lead",
                entity_id=command.lead_id,
                details={
                    key: value
                    for key, value in event_payload.items()
                    if key != "lead_id"
                },
            )
        )
