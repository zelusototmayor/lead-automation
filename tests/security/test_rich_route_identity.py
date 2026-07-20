from __future__ import annotations

import asyncio
import base64
import json
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_principal_settings
from dashboard.app.security import CRMPrincipal, require_crm_principal


WORKSPACE_ID = UUID("11111111-2222-4333-8444-555555555555")
ACTOR_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
USERNAME = "crm-reviewer"
PASSWORD = "correct horse battery staple"
PERMISSIONS = frozenset({"crm:read", "crm:lead-stage:write"})
PRINCIPAL_ENV_NAMES = (
    "CRM_PRINCIPAL_USERNAME",
    "CRM_PRINCIPAL_PASSWORD",
    "CRM_PRINCIPAL_WORKSPACE_ID",
    "CRM_PRINCIPAL_ACTOR_ID",
    "CRM_PRINCIPAL_PERMISSIONS",
    "CRM_PRINCIPAL_IS_ADMIN",
)
RICH_PATHS = (
    "/contas",
    "/propostas",
    "/inteligencia",
    "/operacoes",
    "/api/v1/accounts",
    "/api/v1/proposals",
    "/api/v1/intelligence/recommendations",
    "/api/v1/operations/metrics",
)
LEGACY_READ_PATHS = (
    "/",
    "/dashboard",
    "/cold-calling",
    "/campaign/logistics",
    "/ready",
    "/api/stats",
    "/api/leads",
    "/api/email-followups",
    "/api/outreach-followups",
    "/api/proposal-followups",
    "/api/proposals",
    "/api/impacted-leads",
    "/api/history",
    "/api/account-profiles",
    "/api/portfolio",
    "/api/recommendations",
    "/api/stage-timing",
)


IDENTITY_APP = FastAPI()


@IDENTITY_APP.get("/protected")
async def protected(
    principal: CRMPrincipal = Depends(require_crm_principal),
) -> dict[str, str | bool | list[str]]:
    return {
        "workspace_id": str(principal.workspace_id),
        "actor_id": str(principal.actor_id),
        "subject": principal.subject,
        "permissions": sorted(principal.permissions),
        "is_admin": principal.is_admin,
    }


@pytest.fixture(autouse=True)
def reset_principal_settings(monkeypatch):
    for name in PRINCIPAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    get_principal_settings.cache_clear()
    yield
    get_principal_settings.cache_clear()


def _configured_identity(monkeypatch, *, is_admin: str = "false") -> None:
    monkeypatch.setenv("CRM_PRINCIPAL_USERNAME", USERNAME)
    monkeypatch.setenv("CRM_PRINCIPAL_PASSWORD", PASSWORD)
    monkeypatch.setenv("CRM_PRINCIPAL_WORKSPACE_ID", str(WORKSPACE_ID))
    monkeypatch.setenv("CRM_PRINCIPAL_ACTOR_ID", str(ACTOR_ID))
    monkeypatch.setenv("CRM_PRINCIPAL_PERMISSIONS", ",".join(sorted(PERMISSIONS)))
    monkeypatch.setenv("CRM_PRINCIPAL_IS_ADMIN", is_admin)
    get_principal_settings.cache_clear()


def _raw_asgi_get(
    headers: list[tuple[bytes, bytes]],
) -> tuple[int, dict, dict[str, str]]:
    messages: list[dict] = []
    request_messages = iter(
        (
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        )
    )

    async def receive():
        return next(request_messages)

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/protected",
        "raw_path": b"/protected",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(IDENTITY_APP(scope, receive, send))

    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return start["status"], json.loads(body), response_headers


def test_valid_basic_credentials_return_server_configured_principal(monkeypatch):
    _configured_identity(monkeypatch, is_admin="true")

    response = TestClient(IDENTITY_APP).get("/protected", auth=(USERNAME, PASSWORD))

    assert response.status_code == 200
    assert response.json() == {
        "workspace_id": str(WORKSPACE_ID),
        "actor_id": str(ACTOR_ID),
        "subject": USERNAME,
        "permissions": sorted(PERMISSIONS),
        "is_admin": True,
    }


@pytest.mark.parametrize("is_admin", ("false", "true"))
def test_admin_role_is_derived_exactly_from_server_configuration(monkeypatch, is_admin):
    _configured_identity(monkeypatch, is_admin=is_admin)

    response = TestClient(IDENTITY_APP).get("/protected", auth=(USERNAME, PASSWORD))

    assert response.status_code == 200
    assert response.json()["is_admin"] is (is_admin == "true")


@pytest.mark.parametrize("value", ("TRUE", "False", "1", "yes", " true", "false "))
def test_non_exact_admin_boolean_configuration_fails_closed(monkeypatch, value):
    _configured_identity(monkeypatch, is_admin=value)

    response = TestClient(IDENTITY_APP).get("/protected", auth=(USERNAME, PASSWORD))

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("missing_name", PRINCIPAL_ENV_NAMES)
def test_every_missing_server_identity_value_fails_closed_without_challenge(
    monkeypatch, missing_name
):
    _configured_identity(monkeypatch)
    monkeypatch.delenv(missing_name)
    get_principal_settings.cache_clear()

    response = TestClient(IDENTITY_APP).get("/protected", auth=(USERNAME, PASSWORD))

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "www-authenticate" not in response.headers
    assert USERNAME not in response.text
    assert PASSWORD not in response.text
    assert str(WORKSPACE_ID) not in response.text


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("CRM_PRINCIPAL_USERNAME", ""),
        ("CRM_PRINCIPAL_USERNAME", "   "),
        ("CRM_PRINCIPAL_USERNAME", "invalid:name"),
        ("CRM_PRINCIPAL_USERNAME", "caf\N{LATIN SMALL LETTER E WITH ACUTE}"),
        ("CRM_PRINCIPAL_USERNAME", "line\nbreak"),
        ("CRM_PRINCIPAL_PASSWORD", ""),
        ("CRM_PRINCIPAL_PASSWORD", "   "),
        ("CRM_PRINCIPAL_PASSWORD", "p\N{LATIN SMALL LETTER A WITH DIAERESIS}ssword"),
        ("CRM_PRINCIPAL_PASSWORD", "line\nbreak"),
        ("CRM_PRINCIPAL_WORKSPACE_ID", "not-a-uuid"),
        ("CRM_PRINCIPAL_ACTOR_ID", "not-a-uuid"),
        ("CRM_PRINCIPAL_PERMISSIONS", ""),
        ("CRM_PRINCIPAL_PERMISSIONS", "crm:read,unknown:permission"),
        ("CRM_PRINCIPAL_PERMISSIONS", "crm:read, crm:lead-stage:write"),
        ("CRM_PRINCIPAL_IS_ADMIN", ""),
    ),
)
def test_invalid_server_identity_configuration_fails_closed_generically(
    monkeypatch, name, value
):
    _configured_identity(monkeypatch)
    monkeypatch.setenv(name, value)
    get_principal_settings.cache_clear()

    response = TestClient(IDENTITY_APP).get("/protected", auth=(USERNAME, PASSWORD))

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "www-authenticate" not in response.headers
    assert value not in response.text or value == ""


@pytest.mark.parametrize(
    "auth",
    (
        None,
        ("wrong-user", PASSWORD),
        (USERNAME, "wrong-password"),
    ),
)
def test_missing_or_wrong_credentials_receive_basic_challenge(monkeypatch, auth):
    _configured_identity(monkeypatch)

    response = TestClient(IDENTITY_APP).get("/protected", auth=auth)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["www-authenticate"] == "Basic"
    assert PASSWORD not in response.text


def test_cookie_and_request_tenant_role_inputs_cannot_authenticate(monkeypatch):
    _configured_identity(monkeypatch)

    response = TestClient(IDENTITY_APP).get(
        "/protected",
        params={"workspace_id": str(UUID(int=0)), "is_admin": "true"},
        headers={
            "X-Workspace-ID": str(UUID(int=0)),
            "X-CRM-Role": "admin",
            "Cookie": f"authorization={PASSWORD}; is_admin=true",
        },
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_authenticated_request_cannot_override_server_workspace_or_role(monkeypatch):
    _configured_identity(monkeypatch, is_admin="false")

    client = TestClient(IDENTITY_APP)
    client.cookies.update({"workspace_id": str(UUID(int=0)), "is_admin": "true"})
    response = client.get(
        "/protected",
        auth=(USERNAME, PASSWORD),
        params={"workspace_id": str(UUID(int=0)), "is_admin": "true"},
        headers={"X-Workspace-ID": str(UUID(int=0)), "X-CRM-Role": "admin"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "workspace_id": str(WORKSPACE_ID),
        "actor_id": str(ACTOR_ID),
        "subject": USERNAME,
        "permissions": sorted(PERMISSIONS),
        "is_admin": False,
    }


def test_authenticated_request_cannot_override_server_actor_or_permissions(monkeypatch):
    _configured_identity(monkeypatch)

    response = TestClient(IDENTITY_APP).get(
        "/protected",
        auth=(USERNAME, PASSWORD),
        params={"actor_id": str(UUID(int=0)), "permissions": "crm:proposal:write"},
        headers={
            "X-CRM-Actor-ID": str(UUID(int=0)),
            "X-CRM-Permissions": "crm:proposal:write",
            "Cookie": "actor_id=00000000-0000-0000-0000-000000000000; permissions=*",
        },
    )

    assert response.status_code == 200
    assert response.json()["actor_id"] == str(ACTOR_ID)
    assert response.json()["permissions"] == sorted(PERMISSIONS)


@pytest.mark.parametrize(
    "authorization",
    (
        b"Basic !!!not-base64!!!",
        b"Basic " + base64.b64encode(b"\xff:" + PASSWORD.encode("ascii")),
        b"Basic \xff",
        b"Bearer browser-token",
    ),
)
def test_malformed_non_ascii_or_wrong_scheme_credentials_challenge_without_crashing(
    monkeypatch, authorization
):
    _configured_identity(monkeypatch)

    status_code, body, headers = _raw_asgi_get([(b"authorization", authorization)])

    assert status_code == 401
    assert body == {"detail": "Unauthorized"}
    assert headers["www-authenticate"] == "Basic"


def test_all_rich_routes_challenge_before_feature_or_database_access(monkeypatch):
    _configured_identity(monkeypatch)
    client = TestClient(dashboard_main.app)

    for path in RICH_PATHS:
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"detail": "Unauthorized"}, path
        assert response.headers["www-authenticate"] == "Basic", path


def test_missing_rich_route_config_forbids_without_browser_challenge(monkeypatch):
    client = TestClient(dashboard_main.app)

    response = client.get("/contas", auth=(USERNAME, PASSWORD))

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "www-authenticate" not in response.headers


def test_only_health_remains_public_while_legacy_surface_requires_basic(monkeypatch):
    _configured_identity(monkeypatch)
    client = TestClient(dashboard_main.app)

    health = client.get("/up")
    legacy = client.get("/")
    authenticated_legacy = client.get("/", auth=(USERNAME, PASSWORD))

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert legacy.status_code == 401
    assert legacy.json() == {"detail": "Unauthorized"}
    assert legacy.headers["www-authenticate"] == "Basic"
    assert authenticated_legacy.status_code == 200
    assert "www-authenticate" not in health.headers
    assert USERNAME not in authenticated_legacy.text
    assert PASSWORD not in authenticated_legacy.text


def test_framework_schema_and_documentation_routes_are_not_public(monkeypatch):
    _configured_identity(monkeypatch)
    client = TestClient(dashboard_main.app)

    for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
        response = client.get(path)
        assert response.status_code == 404, path


def test_static_assets_require_the_browser_principal(monkeypatch):
    _configured_identity(monkeypatch)
    client = TestClient(dashboard_main.app)

    unauthenticated = client.get("/static/accounts.js")
    authenticated = client.get("/static/accounts.js", auth=(USERNAME, PASSWORD))

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "Unauthorized"}
    assert unauthenticated.headers["www-authenticate"] == "Basic"
    assert authenticated.status_code == 200
    assert "fetch(" in authenticated.text


def test_every_legacy_read_challenges_before_sheet_or_database_access(monkeypatch):
    _configured_identity(monkeypatch)

    class ExplodingCRM:
        def __getattr__(self, name):
            raise AssertionError(f"legacy CRM was accessed via {name}")

    monkeypatch.setattr(dashboard_main, "crm", ExplodingCRM())
    monkeypatch.setattr(
        dashboard_main,
        "create_database_engine",
        lambda: (_ for _ in ()).throw(AssertionError("database engine was created")),
    )
    client = TestClient(dashboard_main.app)

    for path in LEGACY_READ_PATHS:
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"detail": "Unauthorized"}, path
        assert response.headers["www-authenticate"] == "Basic", path
