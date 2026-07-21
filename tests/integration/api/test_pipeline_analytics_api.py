from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.routers import pipeline as pipeline_router
from dashboard.app.routers.accounts import (
    AccountRequestContext,
    get_account_request_context,
)
from dashboard.app.security import CRMPrincipal
from src.crm.persistence.models import (
    Account,
    Activity,
    Lead,
    Proposal,
    Task,
    Workspace,
)
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def analytics_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, foreign_workspace_id = uuid4(), uuid4()
    account_id, foreign_account_id = uuid4(), uuid4()
    lead_id, second_lead_id, foreign_lead_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 7, 21, 0, 30, tzinfo=UTC)

    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=workspace_id,
                    slug=f"analytics-{workspace_id}",
                    name="Private workspace name",
                    timezone="Europe/Lisbon",
                ),
                Workspace(
                    id=foreign_workspace_id,
                    slug=f"foreign-analytics-{foreign_workspace_id}",
                    name="Foreign private workspace",
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
                    display_name="PII Acme buyer@example.test",
                    normalized_name="pii acme buyer example test",
                ),
                Account(
                    id=foreign_account_id,
                    workspace_id=foreign_workspace_id,
                    display_name="FOREIGN-SECRET-COMPANY",
                    normalized_name="foreign secret company",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Lead(
                    id=lead_id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    stage="contacted",
                ),
                Lead(
                    id=second_lead_id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    stage="new",
                ),
                Lead(
                    id=foreign_lead_id,
                    workspace_id=foreign_workspace_id,
                    account_id=foreign_account_id,
                    stage="won",
                ),
            ]
        )
        session.flush()
        local_proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            lead_id=lead_id,
            title="PRIVATE-PROPOSAL-TITLE",
            status="draft",
            currency="EUR",
        )
        foreign_proposal = Proposal(
            workspace_id=foreign_workspace_id,
            account_id=foreign_account_id,
            lead_id=foreign_lead_id,
            title="FOREIGN-PRIVATE-PROPOSAL",
            status="draft",
            currency="EUR",
        )
        session.add_all([local_proposal, foreign_proposal])
        session.flush()
        session.add_all(
            [
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    activity_type="call",
                    occurred_at=datetime(2026, 7, 20, 21, 30, tzinfo=UTC),
                    title="PRIVATE CALL buyer@example.test",
                    summary="PRIVATE PHONE +351999999999",
                    outcome_code="connected",
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    activity_type="call",
                    occurred_at=datetime(2026, 7, 20, 21, 45, tzinfo=UTC),
                    title="SECOND PRIVATE CALL",
                    outcome_code="voicemail",
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    activity_type="email_sent",
                    occurred_at=datetime(2026, 7, 20, 22, 30, tzinfo=UTC),
                    title="PRIVATE EMAIL SUBJECT",
                    outcome_code="connected",
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=second_lead_id,
                    activity_type="call",
                    occurred_at=datetime(2026, 7, 20, 23, 30, tzinfo=UTC),
                    title="PRIVATE BOUNDARY CALL",
                    outcome_code="customer-secret-outcome",
                ),
                Activity(
                    workspace_id=foreign_workspace_id,
                    account_id=foreign_account_id,
                    lead_id=foreign_lead_id,
                    activity_type="meeting",
                    occurred_at=now,
                    title="FOREIGN-SECRET-ACTIVITY",
                    outcome_code="voicemail",
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="call",
                    title="PRIVATE OVERDUE CALL",
                    due_at=datetime(2026, 7, 20, 22, 0, tzinfo=UTC),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="call",
                    title="PRIVATE TODAY CALL",
                    due_at=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=second_lead_id,
                    task_type="email",
                    title="PRIVATE TODAY EMAIL",
                    due_at=datetime(2026, 7, 21, 1, 0, tzinfo=UTC),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    proposal_id=local_proposal.id,
                    task_type="follow_up",
                    title="PRIVATE PROPOSAL FOLLOWUP",
                    due_at=datetime(2026, 7, 20, 22, 0, tzinfo=UTC),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="email",
                    title="PRIVATE CANCELLED TASK",
                    due_at=now + timedelta(days=2),
                    owner_user_id=uuid4(),
                    status="cancelled",
                ),
                Task(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    task_type="customer-private-task-type",
                    title="PRIVATE CUSTOM TASK TYPE",
                    due_at=now + timedelta(days=3),
                    owner_user_id=uuid4(),
                ),
                Task(
                    workspace_id=foreign_workspace_id,
                    account_id=foreign_account_id,
                    lead_id=foreign_lead_id,
                    task_type="call",
                    title="FOREIGN-SECRET-TASK",
                    due_at=now,
                    owner_user_id=uuid4(),
                ),
            ]
        )

    query_count = 0

    @event.listens_for(engine, "before_cursor_execute")
    def count_queries(*_args):
        nonlocal query_count
        query_count += 1

    def override_context():
        with Session(engine) as session:
            yield AccountRequestContext(
                principal=CRMPrincipal(workspace_id=workspace_id, subject="operator"),
                session=session,
            )

    monkeypatch.setattr(pipeline_router, "_utc_now", lambda: now)
    overrides = dict(dashboard_main.app.dependency_overrides)
    dashboard_main.app.dependency_overrides[get_account_request_context] = (
        override_context
    )
    try:
        yield TestClient(dashboard_main.app), lambda: query_count
    finally:
        dashboard_main.app.dependency_overrides.clear()
        dashboard_main.app.dependency_overrides.update(overrides)
        event.remove(engine, "before_cursor_execute", count_queries)
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, foreign_workspace_id)
        engine.dispose()


def test_pipeline_analytics_returns_only_bounded_workspace_aggregates(analytics_api):
    client, query_count = analytics_api
    before = query_count()

    response = client.get("/api/v1/pipeline/analytics?days=2")

    assert response.status_code == 200
    assert query_count() - before <= 6
    assert response.json() == {
        "period": {
            "start_date": "2026-07-20",
            "end_date": "2026-07-21",
            "days": 2,
        },
        "daily": [
            {
                "date": "2026-07-20",
                "activity_types": {"call": 2, "email_sent": 1},
                "outcomes": {"connected": 2, "voicemail": 1},
                "distinct_touched_leads": 1,
            },
            {
                "date": "2026-07-21",
                "activity_types": {"call": 1},
                "outcomes": {"other": 1},
                "distinct_touched_leads": 1,
            },
        ],
        "stages": {"by_status": {"contacted": 1, "new": 1}, "total": 2},
        "proposals": {"by_status": {"draft": 1}, "total": 1},
        "tasks": {
            "by_status": {"cancelled": 1, "open": 5},
            "open_by_type": {
                "call": 2,
                "email": 1,
                "follow_up": 1,
                "other": 1,
            },
            "total": 6,
        },
        "queues": {
            "counts": {
                "calls_overdue": 1,
                "calls_today": 1,
                "emails_overdue": 0,
                "emails_today": 1,
                "proposal_followups_overdue": 1,
                "proposal_followups_today": 0,
            },
            "unit": "task",
        },
        "time_in_stage": "not_available",
        "generated_at": "2026-07-21T00:30:00Z",
    }

    serialized = response.text
    assert (
        re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            serialized,
            re.IGNORECASE,
        )
        is None
    )
    for secret in (
        "buyer@example.test",
        "+351999999999",
        "PRIVATE",
        "FOREIGN",
        "customer-secret-outcome",
        "customer-private-task-type",
    ):
        assert secret not in serialized


def test_pipeline_analytics_uses_workspace_timezone_day_boundary(analytics_api):
    client, _ = analytics_api

    response = client.get("/api/v1/pipeline/analytics?days=1")

    assert response.status_code == 200
    assert response.json()["daily"] == [
        {
            "date": "2026-07-21",
            "activity_types": {"call": 1},
            "outcomes": {"other": 1},
            "distinct_touched_leads": 1,
        }
    ]


def test_pipeline_analytics_cannot_switch_workspace_from_request_input(analytics_api):
    client, _ = analytics_api
    forged = str(uuid4())

    baseline = client.get("/api/v1/pipeline/analytics?days=2")
    client.cookies.set("workspace_id", forged)
    attempted = client.get(
        f"/api/v1/pipeline/analytics?days=2&workspace_id={forged}",
        headers={"X-Workspace-Id": forged},
    )
    client.cookies.clear()

    assert attempted.status_code == 200
    assert attempted.json() == baseline.json()
    assert attempted.json()["stages"]["by_status"] == {"contacted": 1, "new": 1}


def test_pipeline_analytics_days_are_strictly_bounded(analytics_api):
    client, _ = analytics_api

    default = client.get("/api/v1/pipeline/analytics")
    maximum = client.get("/api/v1/pipeline/analytics?days=120")

    assert default.status_code == maximum.status_code == 200
    assert default.json()["period"]["days"] == 30
    assert maximum.json()["period"]["days"] == 120
    assert client.get("/api/v1/pipeline/analytics?days=0").status_code == 422
    assert client.get("/api/v1/pipeline/analytics?days=121").status_code == 422
    assert client.get("/api/v1/pipeline/analytics?days=thirty").status_code == 422
