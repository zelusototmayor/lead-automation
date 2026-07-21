from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers import tasks as tasks_router
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Account,
    Activity,
    AuditEvent,
    Lead,
    OutboxEvent,
    Task,
    Workspace,
)
from src.crm.services.task_command_service import TaskCommandService
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def task_command_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, task_id, actor_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"task-command-{workspace_id}",
                name="Task command API",
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Task command account",
            normalized_name=f"task command account {workspace_id}",
        )
        session.add(account)
        session.flush()
        session.add(
            Task(
                id=task_id,
                workspace_id=workspace_id,
                account_id=account.id,
                task_type="follow_up",
                title="Follow up",
                due_at=datetime.now(UTC) + timedelta(days=1),
                owner_user_id=actor_id,
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
    monkeypatch.setattr(tasks_router, "_task_engine", lambda: engine)

    principal = CRMPrincipal(
        workspace_id=workspace_id,
        actor_id=actor_id,
        subject="task-command-tester",
        permissions=frozenset({"crm:read", "crm:task:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    try:
        yield TestClient(dashboard_main.app), engine, workspace_id, task_id, actor_id
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


def _workspace_row(session: Session, model, workspace_id):
    return session.scalar(select(model).where(model.workspace_id == workspace_id))


def _workspace_count(session: Session, model, workspace_id) -> int:
    return int(
        session.scalar(
            select(func.count(model.id)).where(model.workspace_id == workspace_id)
        )
        or 0
    )


def test_complete_task_is_atomic_and_creates_completion_activity(task_command_api):
    client, engine, workspace_id, task_id, actor_id = task_command_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/tasks/{task_id}/complete",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json() == {
        "command_id": str(command_id),
        "task_id": str(task_id),
        "version": 2,
        "replayed": False,
    }
    with Session(engine) as session:
        task = session.get(Task, task_id)
        activity = _workspace_row(session, Activity, workspace_id)
        audit = _workspace_row(session, AuditEvent, workspace_id)
        outbox = _workspace_row(session, OutboxEvent, workspace_id)
        assert task.status == "completed"
        assert task.completed_at is not None
        assert task.completion_activity_id == activity.id
        assert activity.activity_type == "task"
        assert activity.title == "Task completed"
        assert activity.actor_id == actor_id
        assert audit.action == "task.completed"
        assert audit.workspace_id == outbox.workspace_id == workspace_id
        assert audit.command_id == outbox.command_id == command_id
        assert outbox.event_type == "task.completed"
        assert _workspace_count(session, Activity, workspace_id) == 1
        assert _workspace_count(session, AuditEvent, workspace_id) == 1
        assert _workspace_count(session, OutboxEvent, workspace_id) == 1


def test_reschedule_task_moves_due_at_and_leaves_task_open(task_command_api):
    client, engine, workspace_id, task_id, actor_id = task_command_api
    command_id = uuid4()
    due_at = datetime.now(UTC) + timedelta(days=3)

    response = client.post(
        f"/api/v1/commands/tasks/{task_id}/reschedule",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "due_at": due_at.isoformat(),
        },
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    with Session(engine) as session:
        task = session.get(Task, task_id)
        activity = _workspace_row(session, Activity, workspace_id)
        audit = _workspace_row(session, AuditEvent, workspace_id)
        outbox = _workspace_row(session, OutboxEvent, workspace_id)
        assert task.status == "open"
        assert task.due_at == due_at
        assert task.completed_at is None
        assert task.completion_activity_id is None
        assert activity.title == "Task rescheduled"
        assert activity.actor_id == actor_id
        assert audit.action == "task.rescheduled"
        assert outbox.event_type == "task.rescheduled"


def test_cancel_task_preserves_null_completion_fields(task_command_api):
    client, engine, workspace_id, task_id, actor_id = task_command_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/tasks/{task_id}/cancel",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    with Session(engine) as session:
        task = session.get(Task, task_id)
        activity = _workspace_row(session, Activity, workspace_id)
        audit = _workspace_row(session, AuditEvent, workspace_id)
        outbox = _workspace_row(session, OutboxEvent, workspace_id)
        assert task.status == "cancelled"
        assert task.completed_at is None
        assert task.completion_activity_id is None
        assert activity.title == "Task cancelled"
        assert activity.actor_id == actor_id
        assert audit.action == "task.cancelled"
        assert outbox.event_type == "task.cancelled"


def test_reschedule_replays_an_equivalent_utc_instant(task_command_api):
    client, engine, workspace_id, task_id, _ = task_command_api
    command_id = uuid4()
    due_at = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)

    first = client.post(
        f"/api/v1/commands/tasks/{task_id}/reschedule",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "due_at": due_at.isoformat(),
        },
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/tasks/{task_id}/reschedule",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "due_at": due_at.astimezone(timezone(timedelta(hours=1))).isoformat(),
        },
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    with Session(engine) as session:
        assert _workspace_count(session, Activity, workspace_id) == 1
        assert _workspace_count(session, AuditEvent, workspace_id) == 1
        assert _workspace_count(session, OutboxEvent, workspace_id) == 1


def test_complete_task_replay_is_idempotent_and_divergence_is_generic(
    task_command_api,
):
    client, engine, workspace_id, task_id, _ = task_command_api
    command_id = uuid4()

    first = client.post(
        f"/api/v1/commands/tasks/{task_id}/complete",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/tasks/{task_id}/complete",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )
    divergent = client.post(
        f"/api/v1/commands/tasks/{task_id}/cancel",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    assert divergent.status_code == 409
    assert divergent.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        assert session.get(Task, task_id).status == "completed"
        assert _workspace_count(session, Activity, workspace_id) == 1
        assert _workspace_count(session, AuditEvent, workspace_id) == 1
        assert _workspace_count(session, OutboxEvent, workspace_id) == 1


def test_task_command_rejects_stale_version_without_writes(task_command_api):
    client, engine, workspace_id, task_id, _ = task_command_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/tasks/{task_id}/complete",
        json={"command_id": str(command_id), "expected_version": 2},
        headers=_headers(command_id),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert (task.status, task.version) == ("open", 1)
        assert _workspace_count(session, Activity, workspace_id) == 0
        assert _workspace_count(session, AuditEvent, workspace_id) == 0
        assert _workspace_count(session, OutboxEvent, workspace_id) == 0


def test_task_command_hides_cross_workspace_task(task_command_api):
    client, engine, workspace_id, task_id, actor_id = task_command_api
    other_workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=other_workspace_id,
                slug=f"task-command-other-{other_workspace_id}",
                name="Other task command workspace",
            )
        )
    principal = CRMPrincipal(
        workspace_id=other_workspace_id,
        actor_id=actor_id,
        subject="other-task-command-tester",
        permissions=frozenset({"crm:read", "crm:task:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/tasks/{task_id}/complete",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert (task.status, task.version) == ("open", 1)
        assert _workspace_count(session, Activity, workspace_id) == 0
        assert _workspace_count(session, AuditEvent, workspace_id) == 0
        assert _workspace_count(session, OutboxEvent, workspace_id) == 0
        session.delete(session.get(Workspace, other_workspace_id))
        session.commit()


@pytest.mark.parametrize(
    ("due_at", "expected_status"),
    [
        ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), 409),
        ((datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None).isoformat(), 422),
    ],
)
def test_reschedule_rejects_past_or_naive_due_at_without_writes(
    task_command_api, due_at, expected_status
):
    client, engine, workspace_id, task_id, _ = task_command_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/tasks/{task_id}/reschedule",
        json={
            "command_id": str(command_id),
            "expected_version": 1,
            "due_at": due_at,
        },
        headers=_headers(command_id),
    )

    assert response.status_code == expected_status
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert (task.status, task.version) == ("open", 1)
        assert _workspace_count(session, Activity, workspace_id) == 0
        assert _workspace_count(session, AuditEvent, workspace_id) == 0
        assert _workspace_count(session, OutboxEvent, workspace_id) == 0


def test_complete_task_supports_pre_account_lead_tasks(task_command_api):
    client, engine, workspace_id, _, actor_id = task_command_api
    lead_id, task_id, command_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(Lead(id=lead_id, workspace_id=workspace_id))
        session.flush()
        session.add(
            Task(
                id=task_id,
                workspace_id=workspace_id,
                account_id=None,
                lead_id=lead_id,
                task_type="call",
                title="Call pre-account lead",
                due_at=datetime.now(UTC) + timedelta(days=1),
                owner_user_id=actor_id,
            )
        )

    response = client.post(
        f"/api/v1/commands/tasks/{task_id}/complete",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    with Session(engine) as session:
        task = session.get(Task, task_id)
        activity = _workspace_row(session, Activity, workspace_id)
        assert task.status == "completed"
        assert activity.account_id is None
        assert activity.lead_id == lead_id


def test_concurrent_identical_completion_replays_once(task_command_api):
    client, engine, workspace_id, task_id, _ = task_command_api
    command_id = uuid4()

    def submit_completion(_index):
        return client.post(
            f"/api/v1/commands/tasks/{task_id}/complete",
            json={"command_id": str(command_id), "expected_version": 1},
            headers=_headers(command_id),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = tuple(pool.map(submit_completion, range(2)))

    assert {response.status_code for response in responses} == {200}
    assert sorted(response.json()["replayed"] for response in responses) == [
        False,
        True,
    ]
    with Session(engine) as session:
        assert session.get(Task, task_id).status == "completed"
        assert _workspace_count(session, Activity, workspace_id) == 1
        assert _workspace_count(session, AuditEvent, workspace_id) == 1
        assert _workspace_count(session, OutboxEvent, workspace_id) == 1


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
def test_task_command_rejects_csrf_or_origin_before_database(monkeypatch, headers):
    monkeypatch.setenv("CRM_CSRF_TOKEN", "csrf-test-token")
    monkeypatch.setenv("CRM_ALLOWED_WRITE_ORIGINS", "http://localhost:8000")
    monkeypatch.setenv("CRM_ENV", "test")
    get_settings.cache_clear()
    principal = CRMPrincipal(
        workspace_id=uuid4(),
        actor_id=uuid4(),
        subject="task-command-tester",
        permissions=frozenset({"crm:read", "crm:task:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    monkeypatch.setattr(
        tasks_router,
        "_task_engine",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )
    try:
        command_id = uuid4()
        response = TestClient(dashboard_main.app).post(
            f"/api/v1/commands/tasks/{uuid4()}/complete",
            json={"command_id": str(command_id), "expected_version": 1},
            headers=headers,
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}
    finally:
        dashboard_main.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_task_command_requires_actor_and_exact_permission(task_command_api):
    client, _, workspace_id, task_id, _ = task_command_api
    for principal in (
        CRMPrincipal(workspace_id=workspace_id, subject="read-only"),
        CRMPrincipal(
            workspace_id=workspace_id,
            actor_id=uuid4(),
            subject="wrong-permission",
            permissions=frozenset({"crm:read", "crm:task:edit"}),
        ),
    ):
        dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: (
            principal
        )
        command_id = uuid4()
        response = client.post(
            f"/api/v1/commands/tasks/{task_id}/complete",
            json={"command_id": str(command_id), "expected_version": 1},
            headers=_headers(command_id),
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}


def test_task_command_rolls_back_domain_activity_audit_and_outbox(
    task_command_api, monkeypatch
):
    client, engine, workspace_id, task_id, _ = task_command_api

    def fail_before_events(*_args, **_kwargs):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(TaskCommandService, "_record_events", fail_before_events)
    command_id = uuid4()
    response = TestClient(client.app, raise_server_exceptions=False).post(
        f"/api/v1/commands/tasks/{task_id}/complete",
        json={"command_id": str(command_id), "expected_version": 1},
        headers=_headers(command_id),
    )

    assert response.status_code == 500
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert (task.status, task.version) == ("open", 1)
        assert task.completed_at is None
        assert task.completion_activity_id is None
        assert (
            session.scalar(
                select(func.count(Activity.id)).where(
                    Activity.workspace_id == workspace_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.workspace_id == workspace_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.workspace_id == workspace_id
                )
            )
            == 0
        )
