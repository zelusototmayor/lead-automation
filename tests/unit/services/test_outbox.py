from __future__ import annotations

from uuid import uuid4

import pytest

from src.crm.ingestion.outbox import enqueue_outbox_event


class _Repository:
    def __init__(self):
        self.added = []

    def add(self, event):
        self.added.append(event)
        return event


class _UnitOfWork:
    def __init__(self):
        self.outbox_events = _Repository()

    def commit(self):  # pragma: no cover - must never be called
        raise AssertionError("enqueue_outbox_event must not commit")


def test_enqueue_outbox_event_uses_caller_transaction_without_publishing():
    uow = _UnitOfWork()
    workspace_id = uuid4()
    command_id = uuid4()
    aggregate_id = uuid4()

    event = enqueue_outbox_event(
        uow,
        workspace_id=workspace_id,
        command_id=command_id,
        semantic_hash="a" * 64,
        event_type="lead.stage_transitioned",
        aggregate_type="lead",
        aggregate_id=aggregate_id,
        payload={"version": 2},
    )

    assert uow.outbox_events.added == [event]
    assert event.workspace_id == workspace_id
    assert event.command_id == command_id
    assert event.aggregate_id == aggregate_id
    assert event.status is None
    assert event.published_at is None


@pytest.mark.parametrize("payload", [{"secret": object()}, ["not", "a", "mapping"]])
def test_enqueue_outbox_event_rejects_non_json_safe_payload_before_database(payload):
    uow = _UnitOfWork()

    with pytest.raises(ValueError, match="^invalid outbox event$"):
        enqueue_outbox_event(
            uow,
            workspace_id=uuid4(),
            command_id=uuid4(),
            semantic_hash="a" * 64,
            event_type="lead.stage_transitioned",
            aggregate_type="lead",
            aggregate_id=uuid4(),
            payload=payload,
        )

    assert uow.outbox_events.added == []
