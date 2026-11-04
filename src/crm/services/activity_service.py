"""Validated, idempotent, append-only activity application service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
import unicodedata
from typing import Any, Callable
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from src.crm.persistence.models import ACTIVITY_TYPES, SOURCE_SYSTEMS
from src.crm.services.account_service import ReplayConflictError


@dataclass(frozen=True, slots=True)
class AppendActivityCommand:
    workspace_id: UUID
    account_id: UUID | None
    activity_type: str
    occurred_at: datetime
    title: str
    lead_id: UUID | None = None
    contact_id: UUID | None = None
    summary: str | None = None
    direction: str | None = None
    source_system: str | None = None
    source_identity_id: UUID | None = None
    ingest_event_id: UUID | None = None
    actor_type: str | None = None
    actor_id: UUID | None = None
    supersedes_activity_id: UUID | None = None
    semantic_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class AppendActivityResult:
    activity_id: UUID


def _safe(
    value: object,
    maximum: int,
    *,
    required: bool = False,
    multiline: bool = False,
) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str:
        raise ValueError("activity requires review") from None
    result = unicodedata.normalize("NFKC", value).strip()
    if not result or len(result) > maximum:
        raise ValueError("activity requires review") from None
    allowed_controls = {"\n", "\r", "\t"} if multiline else set()
    if any(
        unicodedata.category(char) == "Cf"
        or (unicodedata.category(char) == "Cc" and char not in allowed_controls)
        for char in result
    ):
        raise ValueError("activity requires review") from None
    return result


def _values(command: AppendActivityCommand) -> dict[str, object]:
    if type(command) is not AppendActivityCommand:
        raise ValueError("activity requires review") from None
    uuid_fields = (
        "workspace_id",
        "account_id",
        "lead_id",
        "contact_id",
        "source_identity_id",
        "ingest_event_id",
        "actor_id",
        "supersedes_activity_id",
    )
    for name in uuid_fields:
        value = getattr(command, name)
        if value is not None and type(value) is not UUID:
            raise ValueError("activity requires review") from None
    if (
        type(command.occurred_at) is not datetime
        or command.occurred_at.tzinfo is None
        or command.occurred_at.utcoffset() is None
    ):
        raise ValueError("activity requires review") from None
    activity_type = _safe(command.activity_type, 32, required=True)
    if activity_type not in ACTIVITY_TYPES:
        raise ValueError("activity requires review") from None
    if activity_type == "stage_change":
        raise ValueError("activity requires review") from None
    semantic_fingerprint = _safe(command.semantic_fingerprint, 64)
    if (
        semantic_fingerprint is not None
        and re.fullmatch(r"[0-9a-f]{64}", semantic_fingerprint) is None
    ):
        raise ValueError("activity requires review") from None
    if activity_type == "stage_change" and semantic_fingerprint is None:
        raise ValueError("activity requires review") from None
    if command.account_id is None and command.lead_id is None:
        raise ValueError("activity requires review") from None
    if command.contact_id is not None and command.account_id is None:
        raise ValueError("activity requires review") from None
    direction = _safe(command.direction, 32)
    if direction is not None and direction not in {"inbound", "outbound", "internal"}:
        raise ValueError("activity requires review") from None
    source_system = _safe(command.source_system, 32)
    if source_system is not None and source_system not in SOURCE_SYSTEMS:
        raise ValueError("activity requires review") from None
    return {
        "workspace_id": command.workspace_id,
        "account_id": command.account_id,
        "lead_id": command.lead_id,
        "contact_id": command.contact_id,
        "activity_type": activity_type,
        "occurred_at": command.occurred_at.astimezone(UTC),
        "title": _safe(command.title, 512, required=True),
        "summary": _safe(command.summary, 10000, multiline=True),
        "semantic_fingerprint": semantic_fingerprint,
        "direction": direction,
        "source_system": source_system,
        "source_identity_id": command.source_identity_id,
        "ingest_event_id": command.ingest_event_id,
        "actor_type": _safe(command.actor_type, 64),
        "actor_id": command.actor_id,
        "supersedes_activity_id": command.supersedes_activity_id,
    }


def _same(row: Any, values: dict[str, object]) -> bool:
    return all(getattr(row, key) == value for key, value in values.items())


class ActivityService:
    def __init__(self, uow_factory: Callable[[], Any]):
        self.uow_factory = uow_factory

    def append(self, command: AppendActivityCommand) -> AppendActivityResult:
        values = _values(command)
        try:
            with self.uow_factory() as uow:
                replay = None
                if command.ingest_event_id is not None:
                    normalized_type = values["activity_type"]
                    assert isinstance(normalized_type, str)
                    if not uow.lock_activity_replay(
                        command.workspace_id,
                        command.ingest_event_id,
                        normalized_type,
                    ):
                        raise ValueError("activity requires review") from None
                    if hasattr(uow, "activity_replay"):
                        replay = uow.activity_replay(
                            command.workspace_id,
                            command.ingest_event_id,
                            normalized_type,
                        )
                    else:
                        replay = next(
                            (
                                row
                                for row in uow.activities.rows
                                if row.workspace_id == command.workspace_id
                                and row.ingest_event_id == command.ingest_event_id
                                and row.activity_type == normalized_type
                            ),
                            None,
                        )
                if replay is not None:
                    if not _same(replay, values):
                        raise ReplayConflictError(
                            "ingest event already records different semantics"
                        ) from None
                    return AppendActivityResult(replay.id)
                if hasattr(uow, "validate_activity_references"):
                    uow.validate_activity_references(values)
                else:
                    account = (
                        uow.accounts.get(command.workspace_id, command.account_id)
                        if command.account_id is not None
                        else None
                    )
                    if command.account_id is not None and account is None:
                        raise ValueError("activity requires review") from None
                    lead = (
                        uow.leads.get(command.workspace_id, command.lead_id)
                        if command.lead_id is not None
                        else None
                    )
                    if command.lead_id is not None and (
                        lead is None or lead.account_id != command.account_id
                    ):
                        raise ValueError("activity requires review") from None
                    contact = (
                        uow.contacts.get(command.workspace_id, command.contact_id)
                        if command.contact_id is not None
                        else None
                    )
                    if command.contact_id is not None and (
                        contact is None or contact.account_id != command.account_id
                    ):
                        raise ValueError("activity requires review") from None
                    for repository_name, row_id in (
                        ("source_identities", command.source_identity_id),
                        ("activities", command.supersedes_activity_id),
                    ):
                        if row_id is None:
                            continue
                        row = getattr(uow, repository_name).get(
                            command.workspace_id, row_id
                        )
                        if row is None or (
                            repository_name == "activities"
                            and row.account_id != command.account_id
                        ):
                            raise ValueError("activity requires review") from None
                activity = uow.new_activity(**values)
                uow.commit()
                return AppendActivityResult(activity.id)
        except ReplayConflictError:
            raise
        except IntegrityError:
            raise ValueError("activity requires review") from None
        except ValueError:
            raise
