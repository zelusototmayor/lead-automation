"""Transactional persistence for idempotent ingest events and opaque checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import unicodedata
from uuid import UUID, uuid4

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.crm.ingestion.contracts import EventEnvelope
from src.crm.persistence.models import IngestEvent, SyncCheckpoint


class IdempotencyConflictError(RuntimeError):
    """Raised without sensitive context when a transport key changes meaning."""


class InvalidIngestionInputError(ValueError):
    """Raised generically before invalid repository input can reach the database."""


@dataclass(frozen=True, slots=True)
class EventToPersist:
    idempotency_key: str
    envelope: EventEnvelope


@dataclass(frozen=True, slots=True)
class PersistedEventResult:
    event_id: UUID
    duplicate: bool


@dataclass(frozen=True, slots=True)
class CheckpointKey:
    workspace_id: UUID
    connector: str
    source_scope: str
    stream: str


@dataclass(frozen=True, slots=True)
class BatchPersistResult:
    events: tuple[PersistedEventResult, ...]

    @property
    def inserted_count(self) -> int:
        return sum(not event.duplicate for event in self.events)

    @property
    def duplicate_count(self) -> int:
        return sum(event.duplicate for event in self.events)


def _invalid() -> InvalidIngestionInputError:
    return InvalidIngestionInputError("invalid ingestion input")


def _validate_text(value: object, *, max_length: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > max_length:
        raise _invalid()
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise _invalid()
    return value


def _validate_workspace_id(value: object) -> UUID:
    if type(value) is not UUID:
        raise _invalid()
    return value


def _validate_envelope(value: object) -> EventEnvelope:
    if not isinstance(value, EventEnvelope):
        raise _invalid()
    try:
        # ``facts`` intentionally remains a mutable mapping for caller ergonomics.
        # Rebuild a detached contract at this trust boundary so post-validation
        # mutations are either rejected or canonically reflected in persistence.
        python_payload = value.model_dump(
            mode="python", round_trip=True, warnings="error"
        )
        validated = EventEnvelope.model_validate(python_payload)
        return EventEnvelope.model_validate_json(validated.canonical_json())
    except (
        ValidationError,
        PydanticSerializationError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise _invalid() from None


def _normalize_watermark(value: object) -> datetime | None:
    if value is None:
        return None
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _invalid()
    return value.astimezone(UTC)


def record_ingest_event(
    session: Session,
    workspace_id: UUID,
    idempotency_key: str,
    envelope: EventEnvelope,
) -> PersistedEventResult:
    """Insert one append-only event without committing the caller's transaction."""

    if not isinstance(session, Session):
        raise _invalid()
    workspace_id = _validate_workspace_id(workspace_id)
    idempotency_key = _validate_text(idempotency_key, max_length=512)
    envelope = _validate_envelope(envelope)
    payload_hash = envelope.payload_hash()
    event_id = uuid4()
    statement = (
        insert(IngestEvent)
        .values(
            id=event_id,
            workspace_id=workspace_id,
            source_system=envelope.source.system,
            source_scope=envelope.source.scope,
            event_type=envelope.event_type,
            schema_version=envelope.schema_version,
            external_event_id=envelope.source.external_event_id,
            idempotency_key=idempotency_key,
            occurred_at=envelope.occurred_at,
            payload=envelope.persistence_payload(),
            payload_hash=payload_hash,
            correlation_id=envelope.correlation_id,
            causation_id=envelope.causation_id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                IngestEvent.workspace_id,
                IngestEvent.source_system,
                IngestEvent.idempotency_key,
            ]
        )
        .returning(IngestEvent.id)
    )
    inserted_id = session.execute(statement).scalar_one_or_none()
    if inserted_id is not None:
        return PersistedEventResult(event_id=inserted_id, duplicate=False)

    existing = session.execute(
        select(IngestEvent.id, IngestEvent.payload_hash).where(
            IngestEvent.workspace_id == workspace_id,
            IngestEvent.source_system == envelope.source.system,
            IngestEvent.idempotency_key == idempotency_key,
        )
    ).one()
    if existing.payload_hash != payload_hash:
        raise IdempotencyConflictError(
            "idempotency key already records a different event"
        )
    return PersistedEventResult(event_id=existing.id, duplicate=True)


def persist_event_batch_and_advance_checkpoint(
    session: Session,
    key: CheckpointKey,
    next_cursor_encrypted: str,
    events: list[EventToPersist] | tuple[EventToPersist, ...],
    high_watermark_at: datetime | None = None,
) -> BatchPersistResult:
    """Persist a fetched page and advance its opaque cursor in one savepoint.

    Empty pages are successful fetched pages and therefore advance the checkpoint.
    Cursor values remain opaque: this repository neither logs nor compares their order.
    The savepoint ensures callers may catch a batch error while retaining a usable
    outer session; this function never commits the caller-owned transaction.
    """

    if not isinstance(session, Session) or type(key) is not CheckpointKey:
        raise _invalid()
    workspace_id = _validate_workspace_id(key.workspace_id)
    connector = _validate_text(key.connector, max_length=64)
    source_scope = _validate_text(key.source_scope, max_length=255)
    stream = _validate_text(key.stream, max_length=128)
    cursor = _validate_text(next_cursor_encrypted, max_length=65_536)
    watermark = _normalize_watermark(high_watermark_at)
    if type(events) not in {list, tuple}:
        raise _invalid()
    validated_events: list[EventToPersist] = []
    for event in events:
        if type(event) is not EventToPersist:
            raise _invalid()
        validated_events.append(
            EventToPersist(
                idempotency_key=_validate_text(event.idempotency_key, max_length=512),
                envelope=_validate_envelope(event.envelope),
            )
        )

    with session.begin_nested():
        persisted = tuple(
            record_ingest_event(
                session, workspace_id, event.idempotency_key, event.envelope
            )
            for event in validated_events
        )
        now = datetime.now(UTC)
        insert_statement = insert(SyncCheckpoint).values(
            id=uuid4(),
            workspace_id=workspace_id,
            connector=connector,
            source_scope=source_scope,
            stream=stream,
            cursor_encrypted=cursor,
            high_watermark_at=watermark,
            last_success_at=now,
            last_error_redacted=None,
            consecutive_failures=0,
            updated_at=now,
        )
        update_values = {
            "cursor_encrypted": cursor,
            "last_success_at": now,
            "last_error_redacted": None,
            "consecutive_failures": 0,
            "updated_at": now,
        }
        if watermark is not None:
            update_values["high_watermark_at"] = case(
                (
                    SyncCheckpoint.high_watermark_at.is_(None),
                    insert_statement.excluded.high_watermark_at,
                ),
                else_=func.greatest(
                    SyncCheckpoint.high_watermark_at,
                    insert_statement.excluded.high_watermark_at,
                ),
            )
        statement = insert_statement.on_conflict_do_update(
            index_elements=[
                SyncCheckpoint.workspace_id,
                SyncCheckpoint.connector,
                SyncCheckpoint.source_scope,
                SyncCheckpoint.stream,
            ],
            set_=update_values,
        )
        session.execute(statement)

    return BatchPersistResult(events=persisted)
