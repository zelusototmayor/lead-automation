from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers import lead_commands as lead_commands_router
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Account,
    Activity,
    AuditEvent,
    Contact,
    Lead,
    OutboxEvent,
    Task,
    Workspace,
)
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def lead_operations_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, lead_id, actor_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"lead-operations-{workspace_id}",
                name="Lead operations API",
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Original Company",
            normalized_name=f"original company {workspace_id}",
        )
        session.add(account)
        session.flush()
        contact = Contact(
            workspace_id=workspace_id,
            account_id=account.id,
            full_name="Original Contact",
            primary_email=f"original-{workspace_id}@example.com",
            phone="+351210000000",
            is_primary=True,
        )
        session.add(contact)
        session.flush()
        session.add(
            Lead(
                id=lead_id,
                workspace_id=workspace_id,
                account_id=account.id,
                contact_id=contact.id,
                priority="medium",
            )
        )

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
    monkeypatch.setattr(lead_commands_router, "_lead_operation_engine", lambda: engine)
    principal = CRMPrincipal(
        workspace_id=workspace_id,
        actor_id=actor_id,
        subject="lead-operations-tester",
        permissions=frozenset(
            {
                "crm:read",
                "crm:lead:edit",
                "crm:call:log",
                "crm:email:log",
                "crm:note:write",
                "crm:task:write",
            }
        ),
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


def _count(session: Session, model, workspace_id) -> int:
    return int(
        session.scalar(
            select(func.count(model.id)).where(model.workspace_id == workspace_id)
        )
        or 0
    )


def test_edit_lead_unique_collision_returns_generic_conflict_and_rolls_back(
    lead_operations_api,
):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        existing_account = Account(
            workspace_id=workspace_id,
            display_name="Existing Company",
            normalized_name="existing company",
        )
        session.add(existing_account)
        session.flush()
        session.add(
            Contact(
                workspace_id=workspace_id,
                account_id=existing_account.id,
                full_name="Existing Contact",
                primary_email="updated@example.com",
                is_primary=True,
            )
        )
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/edit",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "priority": "high",
            "company_name": "Existing Company",
            "contact_name": "Updated Contact",
            "contact_email": "updated@example.com",
            "contact_phone": "+351****9999",
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        account = session.get(Account, lead.account_id)
        contact = session.get(Contact, lead.contact_id)
        assert lead.version == 1
        assert lead.priority == "medium"
        assert account.display_name == "Original Company"
        assert contact.full_name == "Original Contact"
        assert _count(session, Activity, workspace_id) == 0
        assert _count(session, AuditEvent, workspace_id) == 0
        assert _count(session, OutboxEvent, workspace_id) == 0


def test_edit_lead_updates_priority_company_and_contact_atomically(lead_operations_api):
    client, engine, workspace_id, lead_id, actor_id = lead_operations_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/edit",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "priority": "high",
            "company_name": "Updated Company",
            "contact_name": "Updated Contact",
            "contact_email": "updated@example.com",
            "contact_phone": "+351219999999",
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json() == {
        "command_id": str(command_id),
        "lead_id": str(lead_id),
        "version": 2,
        "replayed": False,
    }
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        account = session.get(Account, lead.account_id)
        contact = session.get(Contact, lead.contact_id)
        assert lead.priority == "high"
        assert account.display_name == "Updated Company"
        assert contact.full_name == "Updated Contact"
        assert str(contact.primary_email) == "updated@example.com"
        assert contact.phone == "+351219999999"
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        )
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        assert activity.title == "Lead details updated"
        assert activity.actor_id == actor_id
        assert audit.action == outbox.event_type == "lead.details_updated"
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_log_call_records_structured_outcome_without_sending(lead_operations_api):
    client, engine, workspace_id, lead_id, actor_id = lead_operations_api
    command_id = uuid4()
    occurred_at = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/log-call",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "outcome_code": "connected",
            "summary": "Asked for a follow-up next week.",
            "occurred_at": occurred_at.isoformat(),
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["occurred_at"] == occurred_at.isoformat().replace(
        "+00:00", "Z"
    )
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        assert lead.version == 2
        assert activity.activity_type == "call"
        assert activity.direction == "outbound"
        assert activity.outcome_code == "connected"
        assert activity.summary == "Asked for a follow-up next week."
        assert activity.actor_id == actor_id
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        assert outbox.event_type == "lead.call_logged"
        assert "summary" not in outbox.payload


def test_log_email_records_manual_activity_without_sending(lead_operations_api):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/log-email",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "direction": "outbound",
            "summary": "Sent requested information manually.",
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    with Session(engine) as session:
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        assert activity.activity_type == "email_sent"
        assert activity.direction == "outbound"
        assert activity.summary == "Sent requested information manually."
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        assert outbox.event_type == "lead.email_logged"
        assert "summary" not in outbox.payload


def test_add_note_records_append_only_private_timeline_activity(lead_operations_api):
    client, engine, workspace_id, lead_id, actor_id = lead_operations_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/add-note",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "summary": "Decision maker wants a shorter implementation window.",
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    with Session(engine) as session:
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        assert activity.activity_type == "note"
        assert activity.title == "Note added"
        assert (
            activity.summary == "Decision maker wants a shorter implementation window."
        )
        assert activity.actor_id == actor_id
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        )
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        assert audit.action == outbox.event_type == "lead.note_added"
        assert "summary" not in outbox.payload
        assert "summary" not in audit.details


def test_schedule_next_action_creates_open_task_and_records_atomic_lead_change(
    lead_operations_api,
):
    client, engine, workspace_id, lead_id, actor_id = lead_operations_api
    command_id = uuid4()
    due_at = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/schedule-next-action",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "task_type": "follow_up",
            "title": "Follow up on requested information",
            "due_at": due_at.isoformat(),
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["command_id"] == str(command_id)
    assert body["lead_id"] == str(lead_id)
    assert body["version"] == 2
    assert body["replayed"] is False
    assert body["task_id"]
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        task = session.get(Task, body["task_id"])
        assert lead.version == 2
        assert task.workspace_id == workspace_id
        assert task.account_id == lead.account_id
        assert task.lead_id == lead_id
        assert task.task_type == "follow_up"
        assert task.title == "Follow up on requested information"
        assert task.due_at == due_at
        assert task.owner_user_id == actor_id
        assert task.status == "open"
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        )
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        assert activity.activity_type == "task"
        assert activity.lead_id == lead_id
        assert audit.action == outbox.event_type == "lead.next_action_scheduled"
        assert outbox.payload["task_id"] == body["task_id"]
        assert _count(session, Task, workspace_id) == 1
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_schedule_next_action_integrity_collision_is_generic_and_atomic(
    lead_operations_api,
):
    _, engine, workspace_id, lead_id, actor_id = lead_operations_api
    command_id = uuid4()
    task_id = uuid5(
        workspace_id,
        f"{command_id}:task:lead.next_action_scheduled",
    )
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        session.add(
            Task(
                id=task_id,
                workspace_id=workspace_id,
                account_id=lead.account_id,
                lead_id=lead_id,
                task_type="call",
                title="Pre-existing collision",
                due_at=datetime.now(UTC) + timedelta(days=1),
                owner_user_id=actor_id,
                status="open",
                source_rule="collision_fixture",
            )
        )
    command_client = TestClient(dashboard_main.app, raise_server_exceptions=False)

    response = command_client.post(
        f"/api/v1/commands/leads/{lead_id}/schedule-next-action",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "task_type": "email",
            "title": "Must roll back",
            "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        assert session.get(Lead, lead_id).version == 1
        assert _count(session, Task, workspace_id) == 1
        assert _count(session, Activity, workspace_id) == 0
        assert _count(session, AuditEvent, workspace_id) == 0
        assert _count(session, OutboxEvent, workspace_id) == 0


def test_schedule_next_action_replays_idempotently_without_duplicate_writes(
    lead_operations_api,
):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    command_id = uuid4()
    payload = {
        "command_id": str(command_id),
        "expected_version": 1,
        "task_type": "call",
        "title": "Call next week",
        "due_at": (datetime.now(UTC) + timedelta(days=7))
        .replace(microsecond=0)
        .isoformat(),
    }

    first = client.post(
        f"/api/v1/commands/leads/{lead_id}/schedule-next-action",
        json=payload,
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/leads/{lead_id}/schedule-next-action",
        json=payload,
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    with Session(engine) as session:
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_log_email_without_explicit_timestamp_replays_original_result(
    lead_operations_api,
):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    command_id = uuid4()
    payload = {
        "command_id": str(command_id),
        "expected_version": 1,
        "direction": "inbound",
        "summary": "Received a manual reply.",
    }

    first = client.post(
        f"/api/v1/commands/leads/{lead_id}/log-email",
        json=payload,
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/leads/{lead_id}/log-email",
        json=payload,
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    with Session(engine) as session:
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_log_email_divergent_replay_fails_closed_without_new_writes(
    lead_operations_api,
):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    command_id = uuid4()
    payload = {
        "command_id": str(command_id),
        "expected_version": 1,
        "direction": "outbound",
        "summary": "Original manual email.",
    }
    assert (
        client.post(
            f"/api/v1/commands/leads/{lead_id}/log-email",
            json=payload,
            headers=_headers(command_id),
        ).status_code
        == 200
    )

    divergent = client.post(
        f"/api/v1/commands/leads/{lead_id}/log-email",
        json=payload | {"summary": "Different manual email."},
        headers=_headers(command_id),
    )

    assert divergent.status_code == 409
    assert divergent.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        assert session.get(Lead, lead_id).version == 2
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_stale_lead_command_fails_closed_without_partial_writes(lead_operations_api):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    first_command_id = uuid4()
    assert (
        client.post(
            f"/api/v1/commands/leads/{lead_id}/log-email",
            json={
                "command_id": str(first_command_id),
                "expected_version": 1,
                "direction": "outbound",
                "summary": "First command wins.",
            },
            headers=_headers(first_command_id),
        ).status_code
        == 200
    )
    stale_command_id = uuid4()

    stale = client.post(
        f"/api/v1/commands/leads/{lead_id}/schedule-next-action",
        json={
            "command_id": str(stale_command_id),
            "expected_version": 1,
            "task_type": "email",
            "title": "This must not be created",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        headers=_headers(stale_command_id),
    )

    assert stale.status_code == 409
    assert stale.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        assert session.get(Lead, lead_id).version == 2
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_cross_workspace_lead_command_fails_closed_without_writes(lead_operations_api):
    client, engine, workspace_id, _, _ = lead_operations_api
    other_workspace_id, other_lead_id = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=other_workspace_id,
                slug=f"cross-workspace-{other_workspace_id}",
                name="Other workspace",
            )
        )
        session.flush()
        account = Account(
            workspace_id=other_workspace_id,
            display_name="Other Company",
            normalized_name=f"other company {other_workspace_id}",
        )
        session.add(account)
        session.flush()
        contact = Contact(
            workspace_id=other_workspace_id,
            account_id=account.id,
            full_name="Other Contact",
            primary_email=f"other-{other_workspace_id}@example.com",
            is_primary=True,
        )
        session.add(contact)
        session.flush()
        session.add(
            Lead(
                id=other_lead_id,
                workspace_id=other_workspace_id,
                account_id=account.id,
                contact_id=contact.id,
                priority="medium",
            )
        )
    command_id = uuid4()
    try:
        response = client.post(
            f"/api/v1/commands/leads/{other_lead_id}/log-email",
            json={
                "command_id": str(command_id),
                "expected_version": 1,
                "direction": "outbound",
                "summary": "Must not cross the workspace boundary.",
            },
            headers=_headers(command_id),
        )

        assert response.status_code == 409
        assert response.json() == {"detail": "Command conflict"}
        with Session(engine) as session:
            assert session.get(Lead, other_lead_id).version == 1
            for tenant_id in (workspace_id, other_workspace_id):
                assert _count(session, Activity, tenant_id) == 0
                assert _count(session, AuditEvent, tenant_id) == 0
                assert _count(session, OutboxEvent, tenant_id) == 0
    finally:
        cleanup_workspace(engine, other_workspace_id)
