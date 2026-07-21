from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers import accounts as accounts_router
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Activity,
    AuditEvent,
    Lead,
    OutboxEvent,
    Workspace,
)
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def lead_command_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, lead_id, actor_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"lead-command-{workspace_id}",
                name="Lead command API",
            )
        )
        session.flush()
        session.add(Lead(id=lead_id, workspace_id=workspace_id))

    for name, value in {
        "CRM_DB_ENABLED": "true",
        "CRM_ACCOUNTS_READ_MODEL": "postgres",
        "CRM_PROPOSALS_READ_MODEL": "postgres",
        "CRM_COMMAND_WRITER": "postgres",
        "CRM_SHEETS_PROJECTION_ENABLED": "false",
        "CRM_AGENT_EVENTS_ENABLED": "false",
        "CRM_CSRF_TOKEN": "csrf-test-token",
        "CRM_ALLOWED_WRITE_ORIGINS": "http://localhost:8000",
        "CRM_ENV": "test",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    get_feature_flags.cache_clear()
    monkeypatch.setattr(accounts_router, "_account_engine", lambda: engine)

    principal = CRMPrincipal(
        workspace_id=workspace_id,
        actor_id=actor_id,
        subject="command-tester",
        permissions=frozenset({"crm:read", "crm:lead-stage:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    try:
        yield TestClient(dashboard_main.app), engine, workspace_id, lead_id, actor_id
    finally:
        dashboard_main.app.dependency_overrides.clear()
        get_settings.cache_clear()
        get_feature_flags.cache_clear()
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def _headers(command_id) -> dict[str, str]:
    return {
        "Origin": "http://localhost:8000",
        "X-CSRF-Token": "csrf-test-token",
        "Idempotency-Key": str(command_id),
    }


def _payload(command_id, **changes):
    payload = {
        "command_id": str(command_id),
        "target_stage": "contacted",
        "expected_version": 1,
        "reviewed_correction": False,
    }
    payload.update(changes)
    return payload


def test_stage_command_is_atomic_audited_and_idempotent(lead_command_api):
    client, engine, workspace_id, lead_id, actor_id = lead_command_api
    command_id = uuid4()

    first = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id),
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id),
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert first.json() == {
        "command_id": str(command_id),
        "lead_id": str(lead_id),
        "version": 2,
        "replayed": False,
    }
    assert replay.json() == first.json() | {"replayed": True}
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        )
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        assert (lead.stage, lead.version) == ("contacted", 2)
        assert activity.activity_type == "stage_change"
        assert activity.lead_id == lead_id
        assert activity.title == "Stage changed"
        assert activity.summary is None
        assert (activity.from_stage, activity.to_stage) == ("new", "contacted")
        assert audit.actor_id == actor_id
        assert audit.workspace_id == outbox.workspace_id == workspace_id
        assert audit.command_id == outbox.command_id == command_id
        assert (
            session.scalar(
                select(func.count(Activity.id)).where(
                    Activity.workspace_id == workspace_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.workspace_id == workspace_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.workspace_id == workspace_id
                )
            )
            == 1
        )


def test_stage_command_requires_matching_idempotency_key(lead_command_api):
    client, engine, _, lead_id, _ = lead_command_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id),
        headers={
            "Origin": "http://localhost:8000",
            "X-CSRF-Token": "csrf-test-token",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid command"}
    with Session(engine) as session:
        assert session.get(Lead, lead_id).version == 1
        assert session.scalar(select(func.count(AuditEvent.id))) == 0
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0


def test_stage_command_conflicts_are_generic_and_do_not_mutate(lead_command_api):
    client, engine, _, lead_id, _ = lead_command_api
    command_id = uuid4()
    assert (
        client.put(
            f"/api/v1/leads/{lead_id}/stage",
            json=_payload(command_id),
            headers=_headers(command_id),
        ).status_code
        == 200
    )

    conflict = client.put(
        f"/api/v1/leads/{lead_id}/stage",
        json=_payload(command_id, target_stage="replied"),
        headers=_headers(command_id),
    )
    missing = client.put(
        f"/api/v1/leads/{uuid4()}/stage",
        json=_payload(uuid4(), expected_version=1),
        headers=_headers(command_id),
    )

    assert conflict.status_code == missing.status_code == 409
    assert conflict.json() == missing.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        assert session.get(Lead, lead_id).stage == "contacted"
        assert session.scalar(select(func.count(AuditEvent.id))) == 1
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://localhost:8000"},
        {"X-CSRF-Token": "csrf-test-token"},
        {
            "Origin": "https://attacker.invalid",
            "X-CSRF-Token": "csrf-test-token",
        },
    ],
)
def test_stage_command_rejects_missing_or_invalid_csrf_origin_before_database(
    monkeypatch, headers
):
    monkeypatch.setenv("CRM_CSRF_TOKEN", "csrf-test-token")
    monkeypatch.setenv("CRM_ALLOWED_WRITE_ORIGINS", "http://localhost:8000")
    monkeypatch.setenv("CRM_ENV", "test")
    get_settings.cache_clear()
    principal = CRMPrincipal(
        workspace_id=uuid4(),
        actor_id=uuid4(),
        subject="command-tester",
        permissions=frozenset({"crm:read", "crm:lead-stage:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    monkeypatch.setattr(
        accounts_router,
        "_account_engine",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )
    try:
        response = TestClient(dashboard_main.app).put(
            f"/api/v1/leads/{uuid4()}/stage",
            json=_payload(uuid4()),
            headers=headers,
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}
    finally:
        dashboard_main.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_stage_command_requires_actor_and_exact_permission(lead_command_api):
    client, _, workspace_id, lead_id, _ = lead_command_api
    for principal in (
        CRMPrincipal(workspace_id=workspace_id, subject="read-only"),
        CRMPrincipal(
            workspace_id=workspace_id,
            actor_id=uuid4(),
            subject="wrong-permission",
            permissions=frozenset({"crm:read", "crm:lead:edit"}),
        ),
    ):
        dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: (
            principal
        )
        command_id = uuid4()
        response = client.put(
            f"/api/v1/leads/{lead_id}/stage",
            json=_payload(command_id),
            headers=_headers(command_id),
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}
