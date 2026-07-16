"""Append-only, tenant-scoped evidence recording."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from src.crm.persistence.models import EVIDENCE_TYPES, Evidence


class EvidenceReviewRequired(RuntimeError):
    """Evidence input cannot safely be persisted without review."""


@dataclass(frozen=True, slots=True)
class RecordEvidenceCommand:
    workspace_id: UUID
    account_id: UUID
    source_identity_id: UUID
    evidence_type: str
    content_hash: str
    captured_at: datetime
    uri: str | None = None
    excerpt_redacted: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sensitivity: str = "confidential"
    retention_until: datetime | None = None


def _review() -> EvidenceReviewRequired:
    return EvidenceReviewRequired("evidence requires review")


class EvidenceService:
    """Record immutable evidence inside a caller-owned transaction."""

    def __init__(self, uow: Any):
        self.uow = uow

    def record(self, command: RecordEvidenceCommand) -> Evidence:
        if type(command) is not RecordEvidenceCommand:
            raise _review()
        if (
            type(command.workspace_id) is not UUID
            or type(command.account_id) is not UUID
        ):
            raise _review()
        if type(command.source_identity_id) is not UUID:
            raise _review()
        if command.evidence_type not in EVIDENCE_TYPES:
            raise _review()
        if (
            type(command.content_hash) is not str
            or len(command.content_hash) != 64
            or any(char not in "0123456789abcdef" for char in command.content_hash)
        ):
            raise _review()
        if not _aware(command.captured_at):
            raise _review()
        if command.retention_until is not None and (
            not _aware(command.retention_until)
            or command.retention_until < command.captured_at
        ):
            raise _review()
        uri = _optional_text(command.uri, 2048)
        excerpt = _optional_text(command.excerpt_redacted, 2048)
        sensitivity = _text(command.sensitivity, 32)
        if type(command.metadata) is not dict:
            raise _review()
        if self.uow.accounts.get(command.workspace_id, command.account_id) is None:
            raise _review()

        existing = self.uow.evidence.by_source(
            command.workspace_id, command.source_identity_id, command.content_hash
        )
        if existing is not None:
            if (
                existing.account_id != command.account_id
                or existing.evidence_type != command.evidence_type
            ):
                raise _review()
            return existing
        return self.uow.evidence.add(
            Evidence(
                workspace_id=command.workspace_id,
                account_id=command.account_id,
                source_identity_id=command.source_identity_id,
                evidence_type=command.evidence_type,
                content_hash=command.content_hash,
                captured_at=command.captured_at,
                uri=uri,
                excerpt_redacted=excerpt,
                metadata_json=dict(command.metadata),
                sensitivity=sensitivity,
                retention_until=command.retention_until,
            )
        )


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _text(value: object, maximum: int) -> str:
    if type(value) is not str:
        raise _review()
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or any(ord(char) < 32 for char in cleaned):
        raise _review()
    return cleaned


def _optional_text(value: object, maximum: int) -> str | None:
    return None if value is None else _text(value, maximum)
