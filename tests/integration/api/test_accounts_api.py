from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from src.crm.persistence.models import Activity


def test_accounts_api_fails_closed_without_trusted_principal():
    response = TestClient(dashboard_main.app).get("/api/v1/accounts")

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_all_accounts_api_reads_fail_closed_without_trusted_principal():
    client = TestClient(dashboard_main.app)

    response = client.get("/api/v1/accounts/00000000-0000-0000-0000-000000000001")

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "www-authenticate" not in response.headers


def test_accounts_list_is_workspace_scoped_paginated_and_aggregate_only(
    account_api_fixture,
):
    client, ids = account_api_fixture

    response = client.get("/api/v1/accounts", params={"limit": 1, "offset": 0})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] in {
        str(ids["account_id"]),
        str(ids["empty_account_id"]),
    }
    serialized = response.text
    assert "ana@example.test" not in serialized
    assert "Private meeting notes" not in serialized
    assert str(ids["foreign_account_id"]) not in serialized


def test_request_input_cannot_select_a_different_workspace(account_api_fixture):
    client, ids = account_api_fixture

    response = client.get(
        "/api/v1/accounts",
        params={"workspace_id": str(uuid4())},
        headers={"X-Workspace-ID": str(uuid4())},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert str(ids["foreign_account_id"]) not in response.text


def test_account_list_query_count_is_constant_for_the_page(account_api_fixture):
    client, ids = account_api_fixture
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(ids["engine"], "before_cursor_execute", record)
    try:
        response = client.get("/api/v1/accounts")
    finally:
        event.remove(ids["engine"], "before_cursor_execute", record)

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(statements) == 2


def test_account_detail_includes_counts_next_action_and_allowlisted_evidence(
    account_api_fixture,
):
    client, ids = account_api_fixture

    response = client.get(f"/api/v1/accounts/{ids['account_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Acme Transport"
    assert body["contact_count"] == 1
    assert body["email_count"] == 2
    assert body["sent_email_count"] == 1
    assert body["received_email_count"] == 1
    assert body["meeting_count"] == 1
    assert body["booked_meeting_count"] == 0
    assert body["held_meeting_count"] == 1
    assert body["cancelled_meeting_count"] == 0
    assert body["no_show_meeting_count"] == 0
    assert body["proposal_count"] == 1
    assert body["probability"] is None
    assert body["next_action"] == "Call buyer"
    assert {item["type"] for item in body["evidence_refs"]} == {
        "email_received",
        "email_sent",
        "meeting",
        "proposal",
        "task",
    }
    assert "summary" not in response.text
    assert "ana@example.test" not in response.text
    assert "Private" not in response.text


def test_account_detail_bounds_recent_evidence(account_api_fixture):
    client, ids = account_api_fixture
    with Session(ids["engine"]) as session, session.begin():
        session.add_all(
            [
                Activity(
                    workspace_id=ids["workspace_id"],
                    account_id=ids["account_id"],
                    activity_type="system",
                    occurred_at=datetime(2026, 7, 15, tzinfo=UTC)
                    + timedelta(minutes=index),
                    title=f"Evidence {index}",
                )
                for index in range(60)
            ]
        )

    response = client.get(f"/api/v1/accounts/{ids['account_id']}")

    assert response.status_code == 200
    assert len(response.json()["evidence_refs"]) == 50


def test_canonical_open_task_is_presented_instead_of_historical_activity(
    account_api_fixture,
):
    client, ids = account_api_fixture

    response = client.get(f"/api/v1/accounts/{ids['account_id']}")

    assert response.status_code == 200
    assert response.json()["next_action"] == "Call buyer"


def test_account_detail_has_explicit_empty_state(account_api_fixture):
    client, ids = account_api_fixture

    response = client.get(f"/api/v1/accounts/{ids['empty_account_id']}")

    assert response.status_code == 200
    assert response.json()["evidence_refs"] == []
    assert response.json()["next_action"] is None
    assert response.json()["probability"] is None


def test_account_detail_blocks_cross_workspace_idor_as_not_found(account_api_fixture):
    client, ids = account_api_fixture

    response = client.get(f"/api/v1/accounts/{ids['foreign_account_id']}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Account not found"}


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({"limit": 0}, 422),
        ({"limit": 101}, 422),
        ({"offset": -1}, 422),
    ],
)
def test_accounts_pagination_is_validated(account_api_fixture, params, expected_status):
    client, _ = account_api_fixture
    assert client.get("/api/v1/accounts", params=params).status_code == expected_status
