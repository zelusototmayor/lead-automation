from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main


def test_proposal_pages_fail_closed_without_trusted_principal():
    client = TestClient(dashboard_main.app)

    assert client.get("/propostas").status_code == 403
    assert (
        client.get("/propostas/00000000-0000-0000-0000-000000000001").status_code == 403
    )


def test_proposals_index_has_filters_totals_states_and_no_recommendations(
    proposal_api_fixture,
):
    client, _ = proposal_api_fixture

    response = client.get("/propostas")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'name="viewport"' in response.text
    assert 'data-state="loading"' in response.text
    assert 'data-state="empty"' in response.text
    assert 'data-state="error"' in response.text
    assert 'data-field="portfolio"' in response.text
    for field in (
        "status",
        "account_id",
        "owner_id",
        "currency",
        "age_min_days",
        "next_action",
        "forecast_category",
        "commercial_vertical",
    ):
        assert f'name="{field}"' in response.text
    assert "/static/proposals.js" in response.text
    assert "recommendation" not in response.text.lower()
    assert "recomend" not in response.text.lower()


def test_proposal_detail_page_is_independent_and_contains_no_rich_data(
    proposal_api_fixture,
):
    client, ids = proposal_api_fixture

    response = client.get(f"/propostas/{ids['confirmed_id']}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert f'data-proposal-id="{ids["confirmed_id"]}"' in response.text
    assert 'data-state="loading"' in response.text
    assert 'data-state="error"' in response.text
    assert "/static/proposals.js" in response.text
    assert "private notes" not in response.text.lower()
    assert "buyer@example.test" not in response.text
    assert "recommendation" not in response.text.lower()


def test_proposal_page_cross_workspace_id_is_not_disclosed(proposal_api_fixture):
    client, ids = proposal_api_fixture
    assert client.get(f"/propostas/{ids['foreign_id']}").status_code == 404
