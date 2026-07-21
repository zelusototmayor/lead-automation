from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class AnalyticsAPI:
    client: TestClient
    query_count: object
    engine: object
    workspace_id: object
    foreign_workspace_id: object
    account_id: object
    foreign_account_id: object
    lead_id: object
    foreign_lead_id: object

    def __iter__(self):
        yield self.client
        yield self.query_count


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
        yield AnalyticsAPI(
            client=TestClient(dashboard_main.app),
            query_count=lambda: query_count,
            engine=engine,
            workspace_id=workspace_id,
            foreign_workspace_id=foreign_workspace_id,
            account_id=account_id,
            foreign_account_id=foreign_account_id,
            lead_id=lead_id,
            foreign_lead_id=foreign_lead_id,
        )
    finally:
        dashboard_main.app.dependency_overrides.clear()
        dashboard_main.app.dependency_overrides.update(overrides)
        event.remove(engine, "before_cursor_execute", count_queries)
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, foreign_workspace_id)
        engine.dispose()


def _stage_change(
    *,
    workspace_id,
    account_id,
    lead_id,
    occurred_at,
    from_stage=None,
    to_stage=None,
):
    return Activity(
        workspace_id=workspace_id,
        account_id=account_id,
        lead_id=lead_id,
        activity_type="stage_change",
        occurred_at=occurred_at,
        title="Stage changed",
        semantic_fingerprint=uuid4().hex * 2,
        from_stage=from_stage,
        to_stage=to_stage,
    )


def test_pipeline_analytics_returns_only_bounded_workspace_aggregates(analytics_api):
    client, query_count = analytics_api
    before = query_count()

    response = client.get("/api/v1/pipeline/analytics?days=2")

    assert response.status_code == 200
    # One fixed aggregate query is reserved for structured time-in-stage coverage.
    assert query_count() - before <= 7
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
        "time_in_stage": {
            "status": "not_available",
            "coverage": {
                "structured_transitions": 0,
                "legacy_transitions": 0,
                "usable_intervals": 0,
                "uncovered_transitions": 0,
            },
            "stages": [],
        },
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


def test_time_in_stage_uses_only_two_contiguous_structured_transitions(
    analytics_api,
):
    with Session(analytics_api.engine) as session, session.begin():
        session.add_all(
            [
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
                    from_stage="new",
                    to_stage="contacted",
                ),
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 20, 30, tzinfo=UTC),
                    from_stage="contacted",
                    to_stage="qualified",
                ),
            ]
        )

    response = analytics_api.client.get("/api/v1/pipeline/analytics?days=2")

    assert response.status_code == 200
    assert response.json()["time_in_stage"] == {
        "status": "available",
        "coverage": {
            "structured_transitions": 2,
            "legacy_transitions": 0,
            "usable_intervals": 1,
            "uncovered_transitions": 1,
        },
        "stages": [
            {
                "stage": "contacted",
                "completed_intervals": 1,
                "average_hours": 12.5,
                "median_hours": 12.5,
                "p90_hours": 12.5,
            }
        ],
    }


def test_time_in_stage_returns_multiple_stages_in_canonical_lifecycle_order(
    analytics_api,
):
    with Session(analytics_api.engine) as session, session.begin():
        session.add_all(
            [
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
                    from_stage="not_a_fit",
                    to_stage="new",
                ),
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
                    from_stage="new",
                    to_stage="contacted",
                ),
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
                    from_stage="contacted",
                    to_stage="qualified",
                ),
            ]
        )

    stage_orders = [
        [
            row["stage"]
            for row in analytics_api.client.get(
                "/api/v1/pipeline/analytics?days=2"
            ).json()["time_in_stage"]["stages"]
        ]
        for _ in range(5)
    ]

    assert stage_orders == [["new", "contacted"]] * 5


def test_time_in_stage_reports_broken_legacy_and_foreign_coverage_without_inference(
    analytics_api,
):
    with Session(analytics_api.engine) as session, session.begin():
        session.add_all(
            [
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 7, 0, tzinfo=UTC),
                    from_stage="new",
                    to_stage="contacted",
                ),
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
                    from_stage="qualified",
                    to_stage="meeting_booked",
                ),
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
                ),
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
                    from_stage="meeting_booked",
                    to_stage="meeting_held",
                ),
                _stage_change(
                    workspace_id=analytics_api.foreign_workspace_id,
                    account_id=analytics_api.foreign_account_id,
                    lead_id=analytics_api.foreign_lead_id,
                    occurred_at=datetime(2026, 7, 20, 7, 0, tzinfo=UTC),
                    from_stage="new",
                    to_stage="contacted",
                ),
                _stage_change(
                    workspace_id=analytics_api.foreign_workspace_id,
                    account_id=analytics_api.foreign_account_id,
                    lead_id=analytics_api.foreign_lead_id,
                    occurred_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
                    from_stage="contacted",
                    to_stage="qualified",
                ),
            ]
        )

    time_in_stage = analytics_api.client.get(
        "/api/v1/pipeline/analytics?days=2"
    ).json()["time_in_stage"]

    assert time_in_stage == {
        "status": "not_available",
        "coverage": {
            "structured_transitions": 3,
            "legacy_transitions": 1,
            "usable_intervals": 0,
            "uncovered_transitions": 3,
        },
        "stages": [],
    }


def test_time_in_stage_is_timezone_independent_and_replay_safe(analytics_api):
    with Session(analytics_api.engine) as session, session.begin():
        session.add_all(
            [
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime.fromisoformat("2026-07-20T10:00:00+05:00"),
                    from_stage="new",
                    to_stage="contacted",
                ),
                _stage_change(
                    workspace_id=analytics_api.workspace_id,
                    account_id=analytics_api.account_id,
                    lead_id=analytics_api.lead_id,
                    occurred_at=datetime.fromisoformat("2026-07-20T04:00:00-04:00"),
                    from_stage="contacted",
                    to_stage="qualified",
                ),
            ]
        )

    first = analytics_api.client.get("/api/v1/pipeline/analytics?days=2")
    replay = analytics_api.client.get("/api/v1/pipeline/analytics?days=2")
    with Session(analytics_api.engine) as session, session.begin():
        session.get(Workspace, analytics_api.workspace_id).timezone = "America/New_York"
    other_timezone = analytics_api.client.get("/api/v1/pipeline/analytics?days=2")

    assert first.status_code == replay.status_code == other_timezone.status_code == 200
    assert first.json()["time_in_stage"] == replay.json()["time_in_stage"]
    assert other_timezone.json()["time_in_stage"] == first.json()["time_in_stage"]
    assert first.json()["time_in_stage"]["stages"][0]["average_hours"] == 3.0
