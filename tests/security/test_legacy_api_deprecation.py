from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_principal_settings


USERNAME = "crm-reviewer"
PASSWORD = "correct horse battery staple"
WORKSPACE_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def configured_identity(monkeypatch):
    monkeypatch.setenv("CRM_PRINCIPAL_USERNAME", USERNAME)
    monkeypatch.setenv("CRM_PRINCIPAL_PASSWORD", PASSWORD)
    monkeypatch.setenv("CRM_PRINCIPAL_WORKSPACE_ID", WORKSPACE_ID)
    monkeypatch.setenv("CRM_PRINCIPAL_IS_ADMIN", "true")
    get_principal_settings.cache_clear()
    yield
    get_principal_settings.cache_clear()


def test_authenticated_legacy_api_responses_are_deprecated_without_contract_changes(
    monkeypatch,
):
    class FakeCRM:
        def get_stats(self, _today):
            return {"total": 7}

    client = TestClient(dashboard_main.app)
    monkeypatch.setattr(dashboard_main, "crm", FakeCRM())
    success = client.get("/api/stats", auth=(USERNAME, PASSWORD))
    monkeypatch.setattr(dashboard_main, "crm", None)

    responses = (
        (success, 200, {"total": 7}),
        (
            client.get("/api/stats", auth=(USERNAME, PASSWORD)),
            503,
            {"error": "PT Logistics CRM not initialized"},
        ),
        (
            client.get("/api/not-a-route", auth=(USERNAME, PASSWORD)),
            404,
            {"detail": "Not Found"},
        ),
    )

    for response, expected_status, expected_body in responses:
        assert response.status_code == expected_status
        assert response.json() == expected_body
        assert response.headers["deprecation"] == "true"


def test_v1_api_and_health_responses_are_not_marked_deprecated():
    client = TestClient(dashboard_main.app)

    responses = (
        (
            client.get("/api/v1/not-a-route", auth=(USERNAME, PASSWORD)),
            404,
            {"detail": "Not Found"},
        ),
        (client.get("/up"), 200, {"status": "ok"}),
    )

    for response, expected_status, expected_body in responses:
        assert response.status_code == expected_status
        assert response.json() == expected_body
        assert "deprecation" not in response.headers


def test_legacy_api_telemetry_contains_only_route_template_and_status(
    caplog, monkeypatch
):
    query_sentinel = "sentinel-secret-query-value"
    path_sentinel = "sentinel-secret-path-value"
    client = TestClient(dashboard_main.app)
    monkeypatch.setattr(dashboard_main, "crm", None)

    with caplog.at_level(logging.INFO, logger=dashboard_main.__name__):
        known_response = client.get("/api/stats", auth=(USERNAME, PASSWORD))
        unknown_response = client.get(
            f"/api/{path_sentinel}",
            params={"token": query_sentinel},
            auth=(USERNAME, PASSWORD),
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == dashboard_main.__name__
        and record.getMessage().startswith("legacy API request")
    ]
    assert known_response.status_code == 503
    assert unknown_response.status_code == 404
    assert messages == [
        "legacy API request path=/api/stats status=503",
        "legacy API request path=<unmatched> status=404",
    ]
    assert all("?" not in message for message in messages)
    assert all("token" not in message for message in messages)
    assert all(query_sentinel not in message for message in messages)
    assert all(path_sentinel not in message for message in messages)
