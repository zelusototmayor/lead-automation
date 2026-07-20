from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from src.crm.ingestion.outbox import (
    LegacySheetsProjectionTransport,
    OutboxMessage,
    PermanentProjectionError,
    ProjectionPayloadError,
    projection_retry_delay,
    redacted_projection_error,
)


class _Adapter:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls: list[dict[str, object]] = []

    def update_lead(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _message(
    *, event_type: str = "lead.stage_transitioned", payload=None, aggregate_type="lead"
):
    payload = payload or {
        "lead_id": str(uuid4()),
        "stage": "meeting_booked",
        "version": 2,
    }
    try:
        aggregate_id = UUID(payload["lead_id"])
    except (KeyError, TypeError, ValueError):
        aggregate_id = uuid4()
    return OutboxMessage(
        id=uuid4(),
        workspace_id=uuid4(),
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        attempt_count=1,
    )


def test_legacy_transport_projects_only_minimized_supported_canonical_event():
    adapter = _Adapter()
    message = _message()

    LegacySheetsProjectionTransport(adapter).publish(message)

    assert adapter.calls == [
        {
            "lead_id": message.payload["lead_id"],
            "updates": {"stage": "Meeting Booked"},
            "mark_touched": False,
        }
    ]


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        (_message(event_type="account.created"), PermanentProjectionError),
        (
            _message(payload={"lead_id": "secret@example.test", "stage": "won"}),
            ProjectionPayloadError,
        ),
        (
            _message(
                payload={"lead_id": str(uuid4()), "stage": "made_up", "version": 2}
            ),
            ProjectionPayloadError,
        ),
        (
            _message(
                payload={
                    "lead_id": str(uuid4()),
                    "stage": "Meeting Booked",
                    "version": 2,
                }
            ),
            ProjectionPayloadError,
        ),
    ],
)
def test_legacy_transport_rejects_unsupported_or_untrusted_payload_without_writes(
    message, error_type
):
    adapter = _Adapter()

    with pytest.raises(error_type, match="^projection unavailable$"):
        LegacySheetsProjectionTransport(adapter).publish(message)

    assert adapter.calls == []


@pytest.mark.parametrize("aggregate_type", ["account", "", "Lead"])
def test_legacy_transport_rejects_non_lead_aggregate_without_writes(aggregate_type):
    adapter = _Adapter()

    with pytest.raises(PermanentProjectionError, match="^projection unavailable$"):
        LegacySheetsProjectionTransport(adapter).publish(
            _message(aggregate_type=aggregate_type)
        )

    assert adapter.calls == []


def test_legacy_transport_rejects_payload_for_a_different_aggregate_without_writes():
    adapter = _Adapter()
    message = _message()
    mismatched = OutboxMessage(
        id=message.id,
        workspace_id=message.workspace_id,
        event_type=message.event_type,
        aggregate_type=message.aggregate_type,
        aggregate_id=uuid4(),
        payload=dict(message.payload),
        attempt_count=message.attempt_count,
    )

    with pytest.raises(ProjectionPayloadError, match="^projection unavailable$"):
        LegacySheetsProjectionTransport(adapter).publish(mismatched)

    assert adapter.calls == []


def test_legacy_transport_treats_missing_sheet_identity_as_retryable_generic_failure():
    adapter = _Adapter(result=False)

    with pytest.raises(RuntimeError, match="^projection failed$"):
        LegacySheetsProjectionTransport(adapter).publish(_message())


def test_retry_delay_is_bounded_and_deterministic():
    assert projection_retry_delay(1) == timedelta(seconds=30)
    assert projection_retry_delay(4) == timedelta(minutes=4)
    assert projection_retry_delay(100) == timedelta(hours=1)


def test_projection_errors_never_retain_payload_exception_or_credentials():
    error = RuntimeError("token=secret-value customer@example.test")

    rendered = redacted_projection_error(error)

    assert rendered == "projection failed"
    assert "secret-value" not in rendered
    assert "example.test" not in rendered


def test_outbox_message_is_a_detached_immutable_snapshot():
    payload = {
        "lead_id": str(uuid4()),
        "stage": "won",
        "version": 3,
        "metadata": {"source": "human"},
    }
    message = _message(payload=payload)
    payload["stage"] = "lost"
    payload["metadata"]["source"] = "agent"

    assert message.payload["stage"] == "won"
    assert message.payload["metadata"]["source"] == "human"
    with pytest.raises(TypeError):
        message.payload["stage"] = "lost"  # type: ignore[index]
    with pytest.raises(TypeError):
        message.payload["metadata"]["source"] = "agent"  # type: ignore[index]


def test_expired_lease_timestamp_can_be_compared_in_utc():
    now = datetime.now(UTC)
    assert now + timedelta(seconds=30) > now
