from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.routers.intelligence import (
    IntelligenceRequestContext,
    get_intelligence_request_context,
)
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import Account, Recommendation, Workspace
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def intelligence_api_fixture():
    engine = create_engine(require_disposable_postgres())
    workspace_id, foreign_workspace_id = uuid4(), uuid4()
    account_id, foreign_account_id = uuid4(), uuid4()
    recommendation_id, foreign_recommendation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=workspace_id, slug=f"intel-{workspace_id}", name="Intelligence"
                ),
                Workspace(
                    id=foreign_workspace_id,
                    slug=f"intel-{foreign_workspace_id}",
                    name="Foreign",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Account(
                    id=account_id,
                    workspace_id=workspace_id,
                    display_name="Acme",
                    normalized_name="acme",
                ),
                Account(
                    id=foreign_account_id,
                    workspace_id=foreign_workspace_id,
                    display_name="Secret Buyer",
                    normalized_name="secret buyer",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Recommendation(
                    id=recommendation_id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    rule_code="inbound_awaiting_response",
                    priority="high",
                    evidence_json=[f"activity:{uuid4()}"],
                    state="open",
                    dedupe_key=f"inbound:{account_id}",
                    observed_at=now,
                ),
                Recommendation(
                    id=foreign_recommendation_id,
                    workspace_id=foreign_workspace_id,
                    account_id=foreign_account_id,
                    rule_code="proposal_missing_next_action",
                    priority="medium",
                    evidence_json=[f"proposal:{uuid4()}"],
                    state="open",
                    dedupe_key=f"proposal:{foreign_account_id}",
                    observed_at=now,
                ),
            ]
        )

    def override():
        with Session(engine) as session:
            yield IntelligenceRequestContext(
                CRMPrincipal(subject="tester", workspace_id=workspace_id), session
            )

    dashboard_main.app.dependency_overrides[get_intelligence_request_context] = override
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: (
        CRMPrincipal(subject="tester", workspace_id=workspace_id)
    )
    try:
        yield (
            TestClient(dashboard_main.app),
            {
                "recommendation_id": recommendation_id,
                "foreign_recommendation_id": foreign_recommendation_id,
                "foreign_workspace_id": foreign_workspace_id,
            },
        )
    finally:
        dashboard_main.app.dependency_overrides.clear()
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, foreign_workspace_id)
        engine.dispose()


def test_intelligence_routes_fail_closed_without_principal():
    client = TestClient(dashboard_main.app)
    for path in ("/inteligencia", "/api/v1/intelligence/recommendations"):
        response = client.get(path)
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}


def test_intelligence_is_separate_scoped_and_redacted(intelligence_api_fixture):
    client, ids = intelligence_api_fixture
    response = client.get(
        "/api/v1/intelligence/recommendations",
        params={"workspace_id": ids["foreign_workspace_id"]},
        headers={"X-Workspace-ID": str(ids["foreign_workspace_id"])},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["rule_code"] == "inbound_awaiting_response"
    assert body["items"][0]["priority"] == "high"
    assert body["items"][0]["evidence"]
    assert body["items"][0]["state"] == "open"
    assert "Secret Buyer" not in response.text
    assert "payload" not in response.text.lower()
    assert "email" not in response.text.lower()


def test_intelligence_detail_blocks_cross_workspace_idor(intelligence_api_fixture):
    client, ids = intelligence_api_fixture
    response = client.get(
        f"/api/v1/intelligence/recommendations/{ids['foreign_recommendation_id']}"
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Recommendation not found"}


def test_intelligence_page_has_loading_empty_and_generic_error_states(
    intelligence_api_fixture,
):
    client, _ = intelligence_api_fixture
    response = client.get("/inteligencia")
    assert response.status_code == 200
    assert "A carregar recomendações" in response.text
    assert "Sem recomendações abertas" in response.text
    assert "Não foi possível carregar Inteligência" not in response.text
    script = client.get("/static/intelligence.js")
    assert "Não foi possível carregar Inteligência" in script.text
    assert "/api/v1/intelligence/recommendations" in script.text


def test_proposals_templates_do_not_contain_recommendation_cards():
    for path in (
        "dashboard/app/templates/proposals/index.html",
        "dashboard/app/templates/proposals/detail.html",
        "dashboard/app/templates/logistics.html",
    ):
        text = Path(path).read_text(encoding="utf-8")
        assert "recommendation" not in text.lower()
