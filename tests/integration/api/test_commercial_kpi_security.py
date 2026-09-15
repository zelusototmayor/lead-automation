"""New routes reuse real automation auth; browser auth remains independently guarded."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from dashboard.app.main import app
from dashboard.app.routers import accounts
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import Lead
from tests.integration.api import test_lead_operations_api as _api_fixtures
from tests.integration.api.test_commercial_kpi_service import proposal, source
from tests.integration.api.test_lead_operations_api import (
    _headers,
)

lead_operations_api = _api_fixtures.lead_operations_api


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/sources"),
        ("GET", "/sources/00000000-0000-0000-0000-000000000001"),
        ("POST", "/assessments"),
        ("POST", "/reconciliations"),
        ("GET", "/reconciliations/00000000-0000-0000-0000-000000000001"),
    ],
)
def test_every_automation_route_auth_timestamp_origin_scope_before_db(
    lead_operations_api, monkeypatch, method, path
):
    client, _engine, workspace, _lead, _actor = lead_operations_api
    token = uuid4().hex + uuid4().hex
    monkeypatch.setenv("CRM_AUTOMATION_BEARER_TOKEN", token)
    monkeypatch.setenv("CRM_AUTOMATION_WORKSPACE_ID", str(workspace))
    monkeypatch.setenv("CRM_AUTOMATION_SCOPES", "work:read,work:write")

    def forbidden_db():
        raise AssertionError("Denied route touched database")

    monkeypatch.setattr(accounts, "_account_engine", forbidden_db)
    url = "/api/v1/agent/commercial-kpis" + path
    headers = {
        "Authorization": "Bearer " + token,
        "X-Agent-Timestamp": datetime.now(UTC).isoformat(),
    }
    assert client.request(method, url, json={}).status_code == 401
    assert (
        client.request(
            method,
            url,
            json={},
            headers=headers | {"X-Agent-Timestamp": "2000-01-01T00:00:00Z"},
        ).status_code
        == 401
    )
    assert (
        client.request(
            method,
            url,
            json={},
            headers=headers | {"Origin": "https://untrusted.example"},
        ).status_code
        == 401
    )
    monkeypatch.setenv("CRM_AUTOMATION_SCOPES", "providers:sync")
    assert client.request(method, url, json={}, headers=headers).status_code == 403


def test_browser_workspace_cursor_origin_scope_and_bounded_commands(
    lead_operations_api, monkeypatch
):
    client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    with Session(engine) as session, session.begin():
        row = session.get(Lead, lead)
        call = source(
            session,
            workspace,
            lead,
            account_id=row.account_id,
            contact_id=row.contact_id,
            summary="Synthetic 👩‍💼 logistical call.",
        )
        source(
            session,
            workspace,
            lead,
            account_id=row.account_id,
            contact_id=row.contact_id,
        )
        call_id = call.id
    base = "/api/v1/pipeline/call-metrics/activities"
    query = "?date=2026-09-15&period=day&limit=1"
    context = client.get(base + "/" + str(call_id)).json()["source"]
    cursor = client.get(base + query).json()["next_cursor"]
    command = uuid4()
    correction = {
        key: value
        for key, value in proposal(context, "no").items()
        if key not in {"activity_id", "rule_version"}
    }
    body = {
        "command_id": str(command),
        "expected_source_digest": context["source_digest"],
        "expected_context_digest": context["context_digest"],
        "expected_override_revision": 0,
        "operation": "set",
        "correction": correction,
    }
    url = base + "/" + str(call_id) + "/correction"
    assert (
        client.post(
            url,
            json=body,
            headers=_headers(command) | {"Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(url, content=b" " * 16385, headers=_headers(command)).status_code
        == 422
    )
    assert (
        client.post(
            url, json=body | {"actor_id": str(actor)}, headers=_headers(command)
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json=body | {"expected_override_revision": True},
            headers=_headers(command),
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json=body | {"command_id": str(command).upper()},
            headers=_headers(command),
        ).status_code
        == 422
    )
    reader = CRMPrincipal(
        workspace_id=workspace,
        actor_id=actor,
        subject="reader",
        permissions=frozenset({"crm:read"}),
    )
    app.dependency_overrides[require_crm_principal] = lambda: reader
    assert client.post(url, json=body, headers=_headers(command)).status_code == 403
    other = CRMPrincipal(
        workspace_id=uuid4(),
        actor_id=actor,
        subject="other-workspace",
        permissions=frozenset({"crm:read", "crm:note:write"}),
    )
    app.dependency_overrides[require_crm_principal] = lambda: other
    assert client.get(base + "/" + str(call_id)).status_code == 404
    assert client.get(base + query + "&cursor=" + cursor).status_code == 400
    assert client.post(url, json=body, headers=_headers(command)).status_code == 404


def test_client_capability_surface_refuses_non_kpi_and_external_urls():
    from tests.integration.api.test_commercial_kpi_model import load_client

    module = load_client()
    transport = object.__new__(module.KPITransport)
    for method, path in [
        ("POST", "/api/v1/commands/leads/x/send"),
        ("GET", "https://untrusted.example/api/v1/agent/commercial-kpis/sources"),
        ("POST", "/api/v1/agent/commercial-kpis/assessments?redirect=x"),
        ("GET", "/api/v1/agent/commercial-kpis/../work"),
        ("POST", "/calendar"),
        ("POST", "/drafts"),
    ]:
        with pytest.raises(module.ModelUnavailable, match="Non-KPI"):
            transport.call(method, path, {})
