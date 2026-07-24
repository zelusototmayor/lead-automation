"""Transactional outbox boundary used by canonical CRM command services."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid5

from src.crm.domain.enums import CRMStage
from src.crm.persistence.models import OutboxEvent


class InvalidOutboxEventError(ValueError):
    """Raised generically before invalid outbox data can reach PostgreSQL."""


class PermanentProjectionError(RuntimeError):
    """An outbox event that the legacy projection deliberately cannot publish."""


class ProjectionPayloadError(PermanentProjectionError):
    """A supported projection event with an invalid minimized payload."""


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """Detached immutable message handed to an external projection transport."""

    id: UUID
    workspace_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    payload: Mapping[str, Any]
    attempt_count: int

    def __post_init__(self) -> None:
        if type(self.payload) is not dict:
            raise ProjectionPayloadError("projection unavailable")
        try:
            snapshot = json.loads(json.dumps(self.payload, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            raise ProjectionPayloadError("projection unavailable") from None
        object.__setattr__(self, "payload", _deep_freeze(snapshot))


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


_STAGE_DISPLAY_NAMES = MappingProxyType(
    {
        "new": "New",
        "contacted": "Contacted",
        "qualified": "Qualified",
        "meeting_booked": "Meeting Booked",
        "meeting_held": "Meeting Held",
        "proposal_requested": "Proposal Requested",
        "proposal_sent": "Proposal Sent",
        "negotiation": "Negotiation",
        "won": "Won",
        "lost": "Lost",
        "not_a_fit": "Not a Fit",
    }
)


class LegacySheetsProjectionTransport:
    """Publish the narrow allowlisted canonical event to the legacy Sheet adapter."""

    def __init__(self, adapter: Any):
        self._adapter = adapter

    def publish(self, message: OutboxMessage) -> None:
        if (
            message.event_type != "lead.stage_transitioned"
            or message.aggregate_type != "lead"
        ):
            raise PermanentProjectionError("projection unavailable")
        payload = message.payload
        try:
            lead_id = UUID(payload["lead_id"])
            stage = CRMStage(payload["stage"]).value
            version = payload["version"]
        except (KeyError, TypeError, ValueError):
            raise ProjectionPayloadError("projection unavailable") from None
        if lead_id != message.aggregate_id or type(version) is not int or version < 1:
            raise ProjectionPayloadError("projection unavailable")
        try:
            updated = self._adapter.update_lead(
                lead_id=str(lead_id),
                updates={"stage": _STAGE_DISPLAY_NAMES[stage]},
                mark_touched=False,
            )
        except Exception:
            raise RuntimeError("projection failed") from None
        if updated is not True:
            raise RuntimeError("projection failed")


def projection_retry_delay(attempt_count: int) -> timedelta:
    """Return deterministic bounded exponential backoff for projection retries."""

    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("invalid attempt count")
    seconds = min(3600, 30 * (2 ** min(attempt_count - 1, 7)))
    return timedelta(seconds=seconds)


def redacted_projection_error(error: BaseException) -> str:
    """Render a fixed non-sensitive persistence value for projection failures."""

    del error
    return "projection failed"


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
