from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_agent_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers.agent_events import get_agent_event_session
from src.crm.persistence.models import Account, IngestEvent, Workspace
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def agent_api(monkeypatch: pytest.MonkeyPatch):
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(id=workspace_id, slug=f"agent-{workspace_id}", name="Agent")
        )

    monkeypatch.setenv("CRM_DB_ENABLED", "true")
    monkeypatch.setenv("CRM_AGENT_EVENTS_ENABLED", "true")
    monkeypatch.setenv("CRM_AGENT_BEARER_TOKEN", "test-agent-secret")
    monkeypatch.setenv("CRM_AGENT_WORKSPACE_ID", str(workspace_id))
    monkeypatch.setenv("CRM_AGENT_SCOPES", "agent-events:write")
    monkeypatch.setenv("CRM_AGENT_SOURCE_SCOPES", "inbox-a")
    monkeypatch.setenv(
        "CRM_AGENT_TOKEN_ISSUED_AT", (now - timedelta(minutes=1)).isoformat()
    )
    monkeypatch.setenv(
        "CRM_AGENT_TOKEN_EXPIRES_AT", (now + timedelta(minutes=10)).isoformat()
    )
    get_agent_settings.cache_clear()
    get_feature_flags.cache_clear()

    def session_override():
        with Session(engine) as session:
            yield session

    dashboard_main.app.dependency_overrides[get_agent_event_session] = session_override
    try:
        yield TestClient(dashboard_main.app), engine, workspace_id, now
    finally:
        dashboard_main.app.dependency_overrides.pop(get_agent_event_session, None)
        get_agent_settings.cache_clear()
        get_feature_flags.cache_clear()
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def event_payload(*, occurred_at: str = "2026-07-15T10:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "event_type": "message.received",
        "source": {
            "system": "agent",
            "scope": "inbox-a",
            "external_event_id": "evt-1",
        },
        "occurred_at": occurred_at,
        "subject": {"kind": "message", "external_id": "msg-1"},
        "account_hint": {"domain": "example.invalid"},
        "facts": {"status": "new", "nested": {"b": 2, "a": 1}},
        "evidence": [{"type": "email_message", "external_id": "ref-1"}],
        "correlation_id": "b840e4d9-6c31-4e7e-a8f6-1db9b3bfed69",
        "causation_id": None,
    }


def headers(now: datetime, **updates: str) -> dict[str, str]:
    result = {
        "Authorization": "Bearer test-agent-secret",
        "Idempotency-Key": "transport-key",
        "X-Agent-Timestamp": now.isoformat(),
    }
    result.update(updates)
    return result


def test_v1_example_is_accepted_and_only_queues_ledger_event(agent_api) -> None:
    client, engine, workspace_id, now = agent_api
    response = client.post(
        "/api/v1/agent-events", json=event_payload(), headers=headers(now)
    )
    assert response.status_code == 202
    assert response.json() == {
        "event_id": response.json()["event_id"],
        "status": "received",
        "duplicate": False,
    }
    with Session(engine) as session:
        event = session.scalar(
            select(IngestEvent).where(IngestEvent.workspace_id == workspace_id)
        )
        assert event is not None
        assert event.processing_status == "received"
        assert (
            session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.workspace_id == workspace_id)
            )
            == 0
        )


def test_replay_normalizes_equivalent_utc_instant_and_returns_same_status(
    agent_api,
) -> None:
    client, engine, workspace_id, now = agent_api
    first = client.post(
        "/api/v1/agent-events", json=event_payload(), headers=headers(now)
    )
    replay = client.post(
        "/api/v1/agent-events",
        json=event_payload(occurred_at="2026-07-15T11:00:00+01:00"),
        headers=headers(now),
    )
    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json() == {
        "event_id": first.json()["event_id"],
        "status": "received",
        "duplicate": True,
    }
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IngestEvent)
                .where(IngestEvent.workspace_id == workspace_id)
            )
            == 1
        )


def test_same_key_with_divergent_normalized_payload_conflicts(agent_api) -> None:
    client, _engine, _workspace_id, now = agent_api
    assert (
        client.post(
            "/api/v1/agent-events", json=event_payload(), headers=headers(now)
        ).status_code
        == 202
    )
    changed = event_payload()
    changed["facts"] = {"status": "changed"}
    response = client.post("/api/v1/agent-events", json=changed, headers=headers(now))
    assert response.status_code == 409
    assert response.json() == {"detail": "Idempotency conflict"}


@pytest.mark.parametrize(
    ("payload", "extra_headers"),
    [
        ({"schema_version": 1}, {}),
        (event_payload() | {"unexpected": "private@example.invalid"}, {}),
        (event_payload(), {"Idempotency-Key": ""}),
    ],
)
def test_invalid_schema_is_generic_422(agent_api, payload, extra_headers) -> None:
    client, _engine, _workspace_id, now = agent_api
    response = client.post(
        "/api/v1/agent-events", json=payload, headers=headers(now, **extra_headers)
    )
    assert response.status_code == 422
    rendered = response.text
    assert rendered == '{"detail":"Invalid request"}'
    assert "private@example.invalid" not in rendered


def test_body_is_bounded_before_validation_or_database(agent_api) -> None:
    client, engine, workspace_id, now = agent_api
    oversized = json.dumps(event_payload() | {"facts": {"blob": "x" * 1_048_576}})
    response = client.post(
        "/api/v1/agent-events",
        content=oversized,
        headers=headers(now) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request"}
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IngestEvent)
                .where(IngestEvent.workspace_id == workspace_id)
            )
            == 0
        )


@pytest.mark.parametrize(
    "header_updates",
    [
        {"Authorization": ""},
        {"Authorization": "Bearer wrong"},
        {"X-Agent-Timestamp": ""},
        {"X-Agent-Timestamp": "2000-01-01T00:00:00Z"},
    ],
)
def test_missing_invalid_or_replayed_auth_is_401(agent_api, header_updates) -> None:
    client, _engine, _workspace_id, now = agent_api
    response = client.post(
        "/api/v1/agent-events",
        json=event_payload(),
        headers=headers(now, **header_updates),
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("CRM_AGENT_SCOPES", "agent-events:read"),
        ("CRM_AGENT_SOURCE_SCOPES", "calendar-a"),
    ],
)
def test_permission_or_source_scope_mismatch_is_403(
    agent_api, monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    client, _engine, _workspace_id, now = agent_api
    monkeypatch.setenv(setting, value)
    get_agent_settings.cache_clear()
    response = client.post(
        "/api/v1/agent-events", json=event_payload(), headers=headers(now)
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_failed_repository_write_rolls_back_caller_transaction(
    agent_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine, workspace_id, now = agent_api

    def fail(*_args, **_kwargs):
        raise RuntimeError("private database detail")

    monkeypatch.setattr("dashboard.app.routers.agent_events.record_ingest_event", fail)
    response = client.post(
        "/api/v1/agent-events", json=event_payload(), headers=headers(now)
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "private database detail" not in response.text
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IngestEvent)
                .where(IngestEvent.workspace_id == workspace_id)
            )
            == 0
        )


def test_authenticated_agent_requests_are_rate_limited_per_principal(agent_api) -> None:
    client, _engine, _workspace_id, now = agent_api

    responses = [
        client.post("/api/v1/agent-events", json=event_payload(), headers=headers(now))
        for _ in range(61)
    ]

    assert responses[0].status_code == 202
    assert all(response.status_code == 200 for response in responses[1:60])
    assert responses[60].status_code == 429
    assert responses[60].json() == {"detail": "Too many requests"}
    assert responses[60].headers["retry-after"] == "60"
