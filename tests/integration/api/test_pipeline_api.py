from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.routers import pipeline as pipeline_router
from dashboard.app.routers.accounts import (
    AccountRequestContext,
    get_account_request_context,
)
from dashboard.app.security import CRMPrincipal
from src.crm.persistence.models import Account, Activity, Contact, Lead, Task, Workspace
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def pipeline_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, other_workspace_id = uuid4(), uuid4()
    account_id, contact_id, lead_id, low_priority_lead_id, pre_account_lead_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    other_account_id, other_lead_id = uuid4(), uuid4()
    now = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=workspace_id,
                    slug=f"pipeline-{workspace_id}",
                    name="Pipeline fixture",
                    timezone="Europe/Lisbon",
                ),
                Workspace(
                    id=other_workspace_id,
                    slug=f"other-{other_workspace_id}",
                    name="Other pipeline fixture",
                    timezone="Europe/Lisbon",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Account(
                    id=account_id,
                    workspace_id=workspace_id,
                    display_name="Acme Logistics",
                    normalized_name="acme logistics",
                    city="Lisboa",
                ),
                Account(
                    id=other_account_id,
                    workspace_id=other_workspace_id,
                    display_name="Foreign Logistics",
                    normalized_name="foreign logistics",
                    city="Lisboa",
                ),
            ]
        )
        session.flush()
        session.add(
            Contact(
                id=contact_id,
                workspace_id=workspace_id,
                account_id=account_id,
                full_name="Ana Silva",
                primary_email="ana@example.test",
                phone="+351210000000",
            )
        )
        session.flush()
        session.add_all(
            [
                Lead(
                    id=lead_id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    contact_id=contact_id,
                    priority="high",
                    stage="contacted",
                ),
                Lead(
                    id=low_priority_lead_id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    priority="low",
                    stage="new",
                ),
                Lead(
                    id=pre_account_lead_id,
                    workspace_id=workspace_id,
                    company_name="Pre Account Company",
                    contact_name="Bruno Prospect",
                    contact_email="bruno@pre-account.example",
                    contact_phone="+351210000001",
                    city="Porto",
                    priority="medium",
                    stage="contacted",
                ),
                Lead(
                    id=other_lead_id,
                    workspace_id=other_workspace_id,
                    account_id=other_account_id,
                    priority="high",
                    stage="contacted",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="call",
                    title="Call today",
                    due_at=now + timedelta(hours=2),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="email",
                    title="Email overdue",
                    due_at=now - timedelta(days=1),
                    owner_user_id=uuid4(),
                ),
                Task(
                    id=UUID(int=1),
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="call",
                    title="First future call",
                    due_at=now + timedelta(days=1),
                    owner_user_id=uuid4(),
                ),
                Task(
                    id=UUID(int=2),
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="call",
                    title="Second future call",
                    due_at=now + timedelta(days=1),
                    owner_user_id=uuid4(),
                ),
                Task(
                    id=UUID(int=3),
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="email",
                    title="Future email",
                    due_at=now + timedelta(days=2),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="email",
                    title="Email at local day end",
                    due_at=datetime(2026, 7, 20, 22, 59, 59, 999999, tzinfo=UTC),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=other_workspace_id,
                    account_id=other_account_id,
                    lead_id=other_lead_id,
                    task_type="email",
                    title="Foreign future email",
                    due_at=now + timedelta(days=1),
                    owner_user_id=uuid4(),
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    contact_id=contact_id,
                    activity_type="call",
                    occurred_at=now - timedelta(hours=1),
                    title="Discovery call",
                    summary="Asked for a proposal",
                    outcome_code="connected",
                    direction="outbound",
                ),
            ]
        )

    def override_context():
        with Session(engine) as session:
            yield AccountRequestContext(
                principal=CRMPrincipal(workspace_id=workspace_id, subject="operator"),
                session=session,
            )

    monkeypatch.setattr(pipeline_router, "_utc_now", lambda: now)
    dashboard_main.app.dependency_overrides[get_account_request_context] = (
        override_context
    )
    try:
        yield TestClient(dashboard_main.app), lead_id, pre_account_lead_id
    finally:
        dashboard_main.app.dependency_overrides.clear()
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, other_workspace_id)
        engine.dispose()


def test_pipeline_summary_and_daily_queues_are_workspace_scoped(pipeline_api):
    client, lead_id, _ = pipeline_api

    summary = client.get("/api/v1/pipeline/summary")
    calls_today = client.get("/api/v1/pipeline/items?queue=calls_today")
    emails_overdue = client.get("/api/v1/pipeline/items?queue=emails_overdue")

    assert summary.status_code == 200
    assert summary.json()["queues"] == {
        "calls_overdue": 0,
        "calls_today": 1,
        "calls_future": 1,
        "emails_overdue": 1,
        "emails_today": 1,
        "emails_future": 1,
        "proposal_followups_overdue": 0,
        "proposal_followups_today": 0,
        "touched_today": 1,
        "untouched": 2,
        "all": 3,
    }
    assert calls_today.status_code == emails_overdue.status_code == 200
    assert calls_today.json()["total"] == emails_overdue.json()["total"] == 1
    item = calls_today.json()["items"][0]
    assert item["lead_id"] == str(lead_id)
    assert item["company"] == "Acme Logistics"
    assert item["contact_name"] == "Ana Silva"
    assert item["phone"] == "+351****0000"
    assert item["priority"] == "high"
    assert item["task"]["type"] == "call"
    assert item["task"]["title"] == "Call today"
    assert item["lead_version"] == 1
    assert item["task"]["version"] == 1


def test_future_call_and_email_queues_are_strict_scoped_and_pagination_safe(
    pipeline_api,
):
    client, lead_id, _ = pipeline_api

    summary = client.get("/api/v1/pipeline/summary")
    first_call = client.get(
        "/api/v1/pipeline/items?queue=calls_future&limit=1&offset=0"
    )
    second_call = client.get(
        "/api/v1/pipeline/items?queue=calls_future&limit=1&offset=1"
    )
    emails = client.get("/api/v1/pipeline/items?queue=emails_future")

    assert summary.status_code == 200
    assert summary.json()["queues"]["calls_future"] == 1
    assert summary.json()["queues"]["emails_future"] == 1
    assert (
        first_call.status_code == second_call.status_code == emails.status_code == 200
    )
    assert first_call.json()["total"] == second_call.json()["total"] == 2
    assert emails.json()["total"] == 1
    assert [
        first_call.json()["items"][0]["task"]["id"],
        second_call.json()["items"][0]["task"]["id"],
    ] == [str(UUID(int=1)), str(UUID(int=2))]
    assert {item["lead_id"] for item in emails.json()["items"]} == {str(lead_id)}
    assert emails.json()["items"][0]["task"]["title"] == "Future email"


def test_pipeline_queue_names_and_pagination_are_strict(pipeline_api):
    client, _, _ = pipeline_api

    assert client.get("/api/v1/pipeline/items?queue=unknown").status_code == 422
    assert client.get("/api/v1/pipeline/items?queue=all&limit=101").status_code == 422
    assert client.get("/api/v1/pipeline/items?queue=all&offset=-1").status_code == 422


def test_pipeline_priority_filter_is_strict_and_applied_before_count(pipeline_api):
    client, lead_id, _ = pipeline_api

    high = client.get("/api/v1/pipeline/items?queue=all&priority=high")
    low = client.get("/api/v1/pipeline/items?queue=all&priority=low")

    assert high.status_code == low.status_code == 200
    assert high.json()["total"] == low.json()["total"] == 1
    assert [item["lead_id"] for item in high.json()["items"]] == [str(lead_id)]
    assert client.get("/api/v1/pipeline/items?priority=urgent").status_code == 422
    assert client.get("/api/v1/pipeline/items?priority=HIGH").status_code == 422
    assert client.get("/api/v1/pipeline/items?priority=").status_code == 422


def test_pipeline_search_includes_city_and_is_workspace_scoped(pipeline_api):
    client, lead_id, pre_account_lead_id = pipeline_api

    by_city = client.get("/api/v1/pipeline/items?queue=all&search=lisboa")
    by_contact = client.get(
        "/api/v1/pipeline/items?queue=all&search=ANA%40EXAMPLE.TEST"
    )
    pre_account = client.get("/api/v1/pipeline/items?queue=all&search=porto")

    assert (
        by_city.status_code == by_contact.status_code == pre_account.status_code == 200
    )
    assert by_city.json()["total"] == 2
    assert str(lead_id) in {item["lead_id"] for item in by_city.json()["items"]}
    assert {item["company"] for item in by_city.json()["items"]} == {"Acme Logistics"}
    assert {item["city"] for item in by_city.json()["items"]} == {"Lisboa"}
    assert [item["lead_id"] for item in by_contact.json()["items"]] == [str(lead_id)]
    assert pre_account.json()["total"] == 1
    assert pre_account.json()["items"][0]["lead_id"] == str(pre_account_lead_id)
    assert pre_account.json()["items"][0]["company"] == "Pre Account Company"
    assert pre_account.json()["items"][0]["account_id"] is None
    assert client.get("/api/v1/pipeline/items?search=%20lisboa").status_code == 422
    assert client.get("/api/v1/pipeline/items?search=").status_code == 422


def test_lead_detail_timeline_and_tasks_preserve_operational_context(pipeline_api):
    client, lead_id, _ = pipeline_api

    detail = client.get(f"/api/v1/leads/{lead_id}")
    timeline = client.get(f"/api/v1/leads/{lead_id}/timeline")
    tasks = client.get(f"/api/v1/leads/{lead_id}/tasks")

    assert detail.status_code == timeline.status_code == tasks.status_code == 200
    detail_payload = detail.json()
    assert detail_payload == {
        "id": str(lead_id),
        "account_id": detail_payload["account_id"],
        "company": "Acme Logistics",
        "contact_name": "Ana Silva",
        "email": "ana@example.test",
        "phone": "+351210000000",
        "city": "Lisboa",
        "stage": "contacted",
        "priority": "high",
        "version": 1,
    }
    assert timeline.json()["total"] == 1
    activity = timeline.json()["items"][0]
    assert activity == {
        "id": activity["id"],
        "type": "call",
        "title": "Discovery call",
        "summary": "Asked for a proposal",
        "outcome_code": "connected",
        "direction": "outbound",
        "occurred_at": "2026-07-20T09:00:00Z",
    }
    assert tasks.json()["total"] == 6
    assert {item["type"] for item in tasks.json()["items"]} == {"call", "email"}
