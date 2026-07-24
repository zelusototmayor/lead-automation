from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from dashboard.app import main as dashboard_main


def test_proposal_reads_fail_closed_without_trusted_principal():
    client = TestClient(dashboard_main.app)

    for path in (
        "/api/v1/proposals",
        "/api/v1/proposals/portfolio",
        "/api/v1/proposals/00000000-0000-0000-0000-000000000001",
    ):
        response = client.get(path)
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}
        assert "www-authenticate" not in response.headers


def test_proposal_list_is_paginated_scoped_filterable_and_redacted(
    proposal_api_fixture,
):
    client, ids = proposal_api_fixture

    response = client.get(
        "/api/v1/proposals",
        params={
            "limit": 1,
            "status": "sent",
            "currency": "eur",
            "account_id": ids["account_id"],
            "owner_id": ids["owner_id"],
            "forecast_category": "commit",
            "next_action": "present",
            "age_min_days": 1,
            "commercial_vertical": "Logistics",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    item = body["items"][0]
    assert item["id"] == str(ids["confirmed_id"])
    assert item["account_name"] == "Acme Transport"
    assert item["value_state"] == "confirmed"
    assert item["one_off_amount"] == "1000.00"
    assert item["mrr_amount"] == "100.00"
    assert item["arr_amount"] is None
    assert item["age_days"] >= 1
    assert item["followup_count"] == 2
    assert item["last_interaction_at"] is not None
    assert item["sent_verification_state"] == "verified"
    assert "private notes" not in response.text.lower()
    assert "buyer@example.test" not in response.text
    assert str(ids["foreign_id"]) not in response.text


def test_request_input_cannot_select_foreign_proposal_workspace(proposal_api_fixture):
    client, ids = proposal_api_fixture

    response = client.get(
        "/api/v1/proposals",
        params={"workspace_id": str(ids["other_workspace_id"])},
        headers={"X-Workspace-ID": str(ids["other_workspace_id"])},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 5
    assert str(ids["foreign_id"]) not in response.text


def test_proposal_list_query_count_is_constant(proposal_api_fixture):
    client, ids = proposal_api_fixture
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(ids["engine"], "before_cursor_execute", record)
    try:
        response = client.get("/api/v1/proposals")
    finally:
        event.remove(ids["engine"], "before_cursor_execute", record)

    assert response.status_code == 200
    assert response.json()["total"] == 5
    assert len(statements) == 2


def test_portfolio_separates_currency_dimensions_and_value_states(
    proposal_api_fixture,
):
    client, _ = proposal_api_fixture

    response = client.get("/api/v1/proposals/portfolio")

    assert response.status_code == 200
    body = response.json()
    assert body["proposal_count"] == 5
    assert body["value_counts"] == {
        "missing": 1,
        "candidate": 1,
        "confirmed": 3,
        "rejected": 0,
    }
    assert body["status_counts"] == {
        "draft": 1,
        "lost": 1,
        "sent": 2,
        "won": 1,
    }
    assert body["totals"] == {
        "EUR": {"one_off": "1000.00", "mrr": "100.00", "arr": "0.00"},
        "GBP": {"one_off": "250.00", "mrr": "0.00", "arr": "0.00"},
        "USD": {"one_off": "500.00", "mrr": "0.00", "arr": "1200.00"},
    }
    assert body["open_pipeline"] == {
        "EUR": {"one_off": "1000.00", "mrr": "100.00", "arr": "0.00"}
    }
    assert body["weighted_pipeline"] == {
        "EUR": {"one_off": "500.00", "mrr": "50.00", "arr": "0.00"}
    }
    assert body["won_totals"] == {
        "USD": {"one_off": "500.00", "mrr": "0.00", "arr": "1200.00"}
    }
    assert body["lost_totals"] == {
        "GBP": {"one_off": "250.00", "mrr": "0.00", "arr": "0.00"}
    }


def test_candidate_and_missing_values_never_enter_totals(proposal_api_fixture):
    client, _ = proposal_api_fixture

    body = client.get("/api/v1/proposals/portfolio").json()

    serialized = str(body["totals"])
    assert "9999.00" not in serialized
    assert body["value_counts"]["missing"] == 1
    assert body["value_counts"]["candidate"] == 1


def test_proposal_detail_has_versions_items_source_and_allowlisted_evidence(
    proposal_api_fixture,
):
    client, ids = proposal_api_fixture

    response = client.get(f"/api/v1/proposals/{ids['confirmed_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == str(ids["account_id"])
    assert len(body["versions"]) == 1
    assert body["versions"][0]["source_document_evidence_id"] == str(
        ids["document_evidence_id"]
    )
    assert body["versions"][0]["confirmed_by"] is None
    assert body["versions"][0]["items"][0]["description"] == "Implementation"
    assert body["sent_evidence_id"] == str(ids["sent_evidence_id"])
    assert body["followups"][0]["activity_id"]
    assert "private notes" not in response.text.lower()
    assert "buyer@example.test" not in response.text


def test_proposal_detail_blocks_cross_workspace_idor(proposal_api_fixture):
    client, ids = proposal_api_fixture

    response = client.get(f"/api/v1/proposals/{ids['foreign_id']}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Proposal not found"}


def test_lost_proposal_exposes_reason_without_raw_evidence(proposal_api_fixture):
    client, ids = proposal_api_fixture

    response = client.get(f"/api/v1/proposals/{ids['lost_id']}")

    assert response.status_code == 200
    assert response.json()["lost_reason"] == "Budget redirected"
    assert "source_document_evidence_id" in response.text


def test_proposal_detail_query_count_is_constant(proposal_api_fixture):
    client, ids = proposal_api_fixture
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(ids["engine"], "before_cursor_execute", record)
    try:
        response = client.get(f"/api/v1/proposals/{ids['confirmed_id']}")
    finally:
        event.remove(ids["engine"], "before_cursor_execute", record)

    assert response.status_code == 200
    assert len(statements) == 5


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"status": "unknown"},
        {"currency": "EURO"},
        {"next_action": "anything"},
        {"age_min_days": -1},
        {"age_max_days": -1},
        {"age_min_days": 20, "age_max_days": 10},
        {"account_id": "not-a-uuid"},
        {"owner_id": "not-a-uuid"},
    ],
)
def test_proposal_filters_and_pagination_are_validated(proposal_api_fixture, params):
    client, _ = proposal_api_fixture
    assert client.get("/api/v1/proposals", params=params).status_code == 422


def test_unknown_proposal_is_not_disclosed(proposal_api_fixture):
    client, _ = proposal_api_fixture
    assert client.get(f"/api/v1/proposals/{uuid4()}").status_code == 404
