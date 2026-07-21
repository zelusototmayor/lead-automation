from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers.proposals import (
    ProposalRequestContext,
    get_proposal_request_context,
)
from dashboard.app.security import CRMPrincipal


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
    assert "data-proposal-pipeline-form" not in response.text
    assert "data-csrf-token" not in response.text


def test_proposal_page_cross_workspace_id_is_not_disclosed(proposal_api_fixture):
    client, ids = proposal_api_fixture
    assert client.get(f"/propostas/{ids['foreign_id']}").status_code == 404


def test_proposal_detail_exposes_operational_form_only_to_authorized_writer(
    proposal_api_fixture, monkeypatch
):
    _, ids = proposal_api_fixture
    client = TestClient(dashboard_main.app, base_url="https://testserver")
    actor_id = uuid4()

    def writable_context():
        with Session(ids["engine"]) as session:
            yield ProposalRequestContext(
                principal=CRMPrincipal(
                    workspace_id=ids["workspace_id"],
                    actor_id=actor_id,
                    subject="proposal-writer",
                    permissions=frozenset({"crm:read", "crm:proposal:write"}),
                ),
                session=session,
            )

    dashboard_main.app.dependency_overrides[get_proposal_request_context] = (
        writable_context
    )
    with monkeypatch.context() as environment:
        for name, value in {
            "CRM_DB_ENABLED": "true",
            "CRM_PROPOSALS_READ_MODEL": "postgres",
            "CRM_COMMAND_WRITER": "postgres",
            "CRM_CSRF_TOKEN": "proposal-page-csrf",
            "CRM_ALLOWED_WRITE_ORIGINS": "https://testserver",
            "CRM_ENV": "test",
        }.items():
            environment.setenv(name, value)
        get_settings.cache_clear()
        get_feature_flags.cache_clear()
        response = client.get(f"/propostas/{ids['confirmed_id']}")
    get_settings.cache_clear()
    get_feature_flags.cache_clear()

    assert response.status_code == 200
    assert 'data-can-write-proposals="true"' in response.text
    assert 'data-csrf-token="proposal-page-csrf"' in response.text
    assert "data-proposal-pipeline-form" in response.text
    for field in (
        "status",
        "probability",
        "forecast_category",
        "next_action",
        "next_action_due_at",
        "lost_reason",
    ):
        assert f'name="{field}"' in response.text
