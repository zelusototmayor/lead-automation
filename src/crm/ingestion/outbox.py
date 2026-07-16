"""Transactional outbox boundary used by canonical CRM command services."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid5

from src.crm.persistence.models import OutboxEvent


class InvalidOutboxEventError(ValueError):
    """Raised generically before invalid outbox data can reach PostgreSQL."""


def _invalid() -> InvalidOutboxEventError:
    return InvalidOutboxEventError("invalid outbox event")


def enqueue_outbox_event(
    uow: Any,
    *,
    workspace_id: UUID,
    command_id: UUID,
    semantic_hash: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: Mapping[str, Any],
) -> OutboxEvent:
    """Add one deterministic message without publishing or committing.

    The caller owns the transaction containing both the domain mutation and this
    row. External projection workers may only act after that transaction commits.
    """

    if (
        type(workspace_id) is not UUID
        or type(command_id) is not UUID
        or type(aggregate_id) is not UUID
        or type(semantic_hash) is not str
        or len(semantic_hash) != 64
        or any(character not in "0123456789abcdef" for character in semantic_hash)
        or type(event_type) is not str
        or not event_type.strip()
        or len(event_type) > 128
        or type(aggregate_type) is not str
        or not aggregate_type.strip()
        or len(aggregate_type) > 64
        or type(payload) is not dict
    ):
        raise _invalid() from None
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None
    if len(encoded) > 4000:
        raise _invalid() from None

    event = OutboxEvent(
        id=uuid5(workspace_id, f"{command_id}:outbox:{event_type}"),
        workspace_id=workspace_id,
        command_id=command_id,
        semantic_hash=semantic_hash,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=dict(payload),
    )
    try:
        uow.outbox_events.add(event)
    except (AttributeError, TypeError):
        raise _invalid() from None
    return event
