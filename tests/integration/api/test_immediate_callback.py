"""Manual callback save must project Calendar before responding, no batch worker."""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from src.crm.persistence.models import Lead, Task, AgentWork
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers


def test_agreed_callback_syncs_on_save_and_replay_does_not_duplicate(lead_operations_api, monkeypatch):
    from src.crm.services import callback_execution
    client, engine, workspace, lead, actor = lead_operations_api
    with Session(engine) as session, session.begin():
        session.get(Lead, lead).company_name = "Callback fixture"
        session.flush()
        version = session.get(Lead, lead).version
    calls = []
    class Calendar:
        def sync_task(self, task, **kwargs):
            # The HTTP action must already be durably committed before Google I/O.
            with Session(engine) as verification:
                assert verification.get(Task, task.id) is not None
            calls.append(str(task.id))
            return {"provider": "google_calendar", "calendar_id": "fixture",
                    "event_id": "crm" + task.id.hex, "status": "scheduled", "verified": True}
    monkeypatch.setattr(callback_execution, "calendar_from_environment", lambda: Calendar())
    command = uuid4()
    body = {"command_id": str(command), "expected_version": version,
            "outcome_code": "connected", "summary": "Callback explicitly agreed",
            "next_action": {"task_type": "call", "title": "Call back",
                            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                            "call_intent": {"schema_version": 1, "purpose": "agreed_callback",
                              "agreed_with_client": True, "calendar_policy": "none",
                              "obligation_key": str(uuid4())}}}
    path = f"/api/v1/commands/leads/{lead}/log-call"
    response = client.post(path, json=body, headers=_headers(command))
    assert response.status_code == 200, response.text
    assert response.json()["callback_sync_status"] == "synced"
    task_id = response.json()["task_id"]
    replay = client.post(path, json=body, headers=_headers(command))
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["callback_sync_status"] == "synced"
    assert calls == [task_id]
    with Session(engine) as session:
        task = session.get(Task, UUID(task_id))
        assert task.call_intent == body["next_action"]["call_intent"] | {"evidence_refs": [], "gate_reason": None}
        work = session.scalars(select(AgentWork).where(AgentWork.workspace_id == workspace,
                                AgentWork.kind == "calendar_callback")).all()
        assert len(work) == 1
        assert work[0].status == "completed"
        from src.crm.persistence.models import AuditEvent
        audit = session.scalar(select(AuditEvent).where(AuditEvent.workspace_id == workspace,
                               AuditEvent.action == "lead.call_logged"))
        assert audit.details["callback_sync_status"] == "pending"  # status at save time


import pytest

@pytest.mark.parametrize("intent", [None, "agreed_callback", "internal_preparation"])
def test_schedule_and_lifecycle_project_only_callbacks(lead_operations_api, monkeypatch, intent):
    from src.crm.services import callback_execution
    from dashboard.app.routers import tasks as task_router
    from tests.unit.services.test_canonical_callback import Calendar
    client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(task_router, "_task_engine", lambda: engine)
    calendar = Calendar()
    monkeypatch.setattr(callback_execution, "calendar_from_environment", lambda: calendar)
    with Session(engine) as session, session.begin():
        session.get(Lead, lead).company_name = "Callback fixture"
        session.flush()
        version = session.get(Lead, lead).version
    command = uuid4()
    due = datetime.now(UTC) + timedelta(days=1)
    body = {"command_id": str(command), "expected_version": version,
            "task_type": "call", "title": "Call back", "due_at": due.isoformat()}
    if intent:
        body["call_intent"] = dict(schema_version=1, purpose=intent,
            agreed_with_client=intent == "agreed_callback", calendar_policy="none",
            obligation_key=str(uuid4()))
    response = client.post(f"/api/v1/commands/leads/{lead}/schedule-next-action",
                           json=body, headers=_headers(command))
    assert response.status_code == 200, response.text
    needed = intent != "internal_preparation"
    assert response.json()["callback_sync_status"] == ("synced" if needed else "not_required")
    task_id = UUID(response.json()["task_id"])
    event_id = "crm" + task_id.hex
    assert (event_id in calendar.events) is needed
    if needed:
        assert calendar.events[event_id]["summary"] == "Callback fixture"
        assert not calendar.events[event_id].get("attendees")
        assert datetime.fromisoformat(calendar.events[event_id]["start"]["dateTime"]) == due
    due += timedelta(hours=1)
    for operation, version in [("reschedule", 1), ("cancel", 2)]:
        command = uuid4()
        body = {"command_id": str(command), "expected_version": version}
        if operation == "reschedule":
            body["due_at"] = due.isoformat()
        response = client.post(f"/api/v1/commands/tasks/{task_id}/{operation}",
                               json=body, headers=_headers(command))
        assert response.status_code == 200, response.text
        if operation == "reschedule" and needed:
            assert len(calendar.events) == 1
            assert datetime.fromisoformat(calendar.events[event_id]["start"]["dateTime"]) == due
    assert not calendar.events


def test_recover_existing_callback_without_relogging_or_changing_task(lead_operations_api, monkeypatch):
    from src.crm.services import callback_execution
    from dashboard.app.routers import tasks as task_router
    from tests.unit.services.test_canonical_callback import Calendar
    client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(task_router, "_task_engine", lambda: engine)
    calendar = Calendar()
    monkeypatch.setattr(callback_execution, "calendar_from_environment", lambda: calendar)
    with Session(engine) as session, session.begin():
        original = session.get(Lead, lead)
        original.company_name = "Recovery fixture"
        task = Task(workspace_id=workspace, lead_id=lead, account_id=original.account_id,
                    task_type="call", status="open", title="Already saved callback",
                    due_at=datetime.now(UTC) + timedelta(days=1), owner_user_id=actor,
                    call_intent=dict(schema_version=1, purpose="agreed_callback",
                        agreed_with_client=True, calendar_policy="none", obligation_key=str(uuid4())))
        session.add(task)
        session.flush()
        task_id = task.id
    command = uuid4()
    body = dict(command_id=str(command), expected_version=1)
    path = f"/api/v1/commands/tasks/{task_id}/sync-calendar"
    response = client.post(path, json=body, headers=_headers(command))
    assert response.status_code == 200, response.text
    assert response.json()["callback_sync_status"] == "synced"
    replay = client.post(path, json=body, headers=_headers(command))
    assert replay.status_code == 200
    assert len(calendar.events) == 1
    with Session(engine) as session:
        assert session.get(Task, task_id).version == 1
    conflict = client.post(path, json=body | {"expected_version": 99}, headers=_headers(command))
    assert conflict.status_code == 409
    assert client.post(path, json=body).status_code in (401, 403, 422)


def test_provider_failure_keeps_save_and_pending_work(lead_operations_api, monkeypatch):
    from src.crm.services import callback_execution
    client, engine, workspace, lead, actor = lead_operations_api
    with Session(engine) as session, session.begin():
        session.get(Lead, lead).company_name = "Outage fixture"
        session.flush()
        version = session.get(Lead, lead).version
    class BrokenCalendar:
        def sync_task(self, task, **kwargs):
            raise TimeoutError("provider unavailable")
    monkeypatch.setattr(callback_execution, "calendar_from_environment", lambda: BrokenCalendar())
    command = uuid4()
    body = dict(command_id=str(command), expected_version=version, task_type="call",
                title="Callback", due_at=(datetime.now(UTC)+timedelta(days=1)).isoformat())
    path = f"/api/v1/commands/leads/{lead}/schedule-next-action"
    response = client.post(path, json=body, headers=_headers(command))
    assert response.status_code == 200
    assert response.json()["callback_sync_status"] == "pending"
    assert client.post(path, json=body, headers=_headers(command)).status_code == 200
    with Session(engine) as session:
        assert len(session.scalars(select(Task).where(Task.workspace_id == workspace)).all()) == 1
        rows = session.scalars(select(AgentWork).where(AgentWork.workspace_id == workspace,
                                  AgentWork.kind == "calendar_callback")).all()
        assert len(rows) == 1 and rows[0].status == "queued" and rows[0].attempts == 1
