from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def _clear_flags(monkeypatch):
    for name in (
        "CRM_DB_ENABLED",
        "CRM_ACCOUNTS_READ_MODEL",
        "CRM_PROPOSALS_READ_MODEL",
        "CRM_COMMAND_WRITER",
        "CRM_SHEETS_PROJECTION_ENABLED",
        "CRM_AGENT_EVENTS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_cutover_flags_default_to_legacy_fail_closed(monkeypatch):
    from dashboard.app.feature_flags import load_feature_flags

    _clear_flags(monkeypatch)

    flags = load_feature_flags()

    assert flags.database_enabled is False
    assert flags.accounts_read_model == "legacy"
    assert flags.proposals_read_model == "legacy"
    assert flags.command_writer == "sheet"
    assert flags.sheets_projection_enabled is False
    assert flags.agent_events_enabled is False


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("CRM_DB_ENABLED", "1"),
        ("CRM_DB_ENABLED", "TRUE"),
        ("CRM_DB_ENABLED", " true "),
        ("CRM_SHEETS_PROJECTION_ENABLED", "yes"),
        ("CRM_AGENT_EVENTS_ENABLED", "enabled"),
        ("CRM_ACCOUNTS_READ_MODEL", "primary"),
        ("CRM_ACCOUNTS_READ_MODEL", "POSTGRES"),
        ("CRM_PROPOSALS_READ_MODEL", " sheet "),
        ("CRM_PROPOSALS_READ_MODEL", "sheet"),
        ("CRM_COMMAND_WRITER", "dual"),
    ),
)
def test_cutover_flags_reject_unknown_values(monkeypatch, name, value):
    from dashboard.app.feature_flags import load_feature_flags

    _clear_flags(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="invalid CRM_"):
        load_feature_flags()


@pytest.mark.parametrize(
    "unsafe",
    (
        {"CRM_ACCOUNTS_READ_MODEL": "shadow"},
        {"CRM_ACCOUNTS_READ_MODEL": "postgres"},
        {"CRM_PROPOSALS_READ_MODEL": "shadow"},
        {"CRM_PROPOSALS_READ_MODEL": "postgres"},
        {"CRM_COMMAND_WRITER": "postgres"},
        {"CRM_SHEETS_PROJECTION_ENABLED": "true"},
        {"CRM_AGENT_EVENTS_ENABLED": "true"},
    ),
)
def test_database_features_cannot_activate_without_database(monkeypatch, unsafe):
    from dashboard.app.feature_flags import load_feature_flags

    _clear_flags(monkeypatch)
    for name, value in unsafe.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="unsafe CRM cutover configuration"):
        load_feature_flags()


def test_sheets_projection_requires_postgres_writer(monkeypatch):
    from dashboard.app.feature_flags import load_feature_flags

    _clear_flags(monkeypatch)
    monkeypatch.setenv("CRM_DB_ENABLED", "true")
    monkeypatch.setenv("CRM_SHEETS_PROJECTION_ENABLED", "true")

    with pytest.raises(ValueError, match="unsafe CRM cutover configuration"):
        load_feature_flags()


def test_consistent_shadow_and_postgres_configuration_is_accepted(monkeypatch):
    from dashboard.app.feature_flags import load_feature_flags

    _clear_flags(monkeypatch)
    monkeypatch.setenv("CRM_DB_ENABLED", "true")
    monkeypatch.setenv("CRM_ACCOUNTS_READ_MODEL", "shadow")
    monkeypatch.setenv("CRM_PROPOSALS_READ_MODEL", "postgres")
    monkeypatch.setenv("CRM_COMMAND_WRITER", "postgres")
    monkeypatch.setenv("CRM_SHEETS_PROJECTION_ENABLED", "true")
    monkeypatch.setenv("CRM_AGENT_EVENTS_ENABLED", "true")

    flags = load_feature_flags()

    assert flags.database_enabled is True
    assert flags.accounts_read_model == "shadow"
    assert flags.proposals_read_model == "postgres"
    assert flags.command_writer == "postgres"
    assert flags.sheets_projection_enabled is True
    assert flags.agent_events_enabled is True


def test_disabled_agent_event_route_does_not_resolve_auth_or_database(monkeypatch):
    from dashboard.app import main as dashboard_main
    from dashboard.app.feature_flags import get_feature_flags
    from dashboard.app.routers import agent_events

    _clear_flags(monkeypatch)
    get_feature_flags.cache_clear()

    def forbidden_session():
        raise AssertionError("disabled ingress must not open PostgreSQL")
        yield

    def forbidden_auth(_request):
        raise AssertionError("disabled ingress must not resolve agent auth")

    async def forbidden_body(_request):
        raise AssertionError("disabled ingress must not parse the request body")

    dashboard_main.app.dependency_overrides[agent_events.get_agent_event_session] = (
        forbidden_session
    )
    monkeypatch.setattr(agent_events, "_authenticate", forbidden_auth)
    monkeypatch.setattr(agent_events, "_bounded_json", forbidden_body)
    try:
        response = TestClient(dashboard_main.app).post(
            "/api/v1/agent-events", json={"private": "must-not-be-parsed"}
        )
    finally:
        dashboard_main.app.dependency_overrides.pop(
            agent_events.get_agent_event_session, None
        )
        get_feature_flags.cache_clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_shadow_account_route_does_not_open_postgres(monkeypatch):
    from dashboard.app import main as dashboard_main
    from dashboard.app.feature_flags import get_feature_flags
    from dashboard.app.routers import accounts
    from dashboard.app.security import CRMPrincipal, require_crm_principal

    _clear_flags(monkeypatch)
    monkeypatch.setenv("CRM_DB_ENABLED", "true")
    monkeypatch.setenv("CRM_ACCOUNTS_READ_MODEL", "shadow")
    get_feature_flags.cache_clear()
    opened = False

    def forbidden_engine():
        nonlocal opened
        opened = True
        raise AssertionError("shadow reads must not open PostgreSQL for user traffic")

    async def principal_override():
        return CRMPrincipal(workspace_id=uuid4(), subject="cutover-test")

    monkeypatch.setattr(accounts, "_account_engine", forbidden_engine)
    dashboard_main.app.dependency_overrides[require_crm_principal] = principal_override
    try:
        response = TestClient(dashboard_main.app).get("/api/v1/accounts")
    finally:
        dashboard_main.app.dependency_overrides.pop(require_crm_principal, None)
        get_feature_flags.cache_clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Accounts unavailable"}
    assert opened is False


@pytest.mark.parametrize(
    ("module_name", "path", "engine_name", "detail", "is_admin"),
    (
        (
            "intelligence",
            "/api/v1/intelligence/recommendations",
            "_intelligence_engine",
            "Intelligence unavailable",
            False,
        ),
        (
            "operations",
            "/api/v1/operations/metrics",
            "_operations_engine",
            "Operations unavailable",
            True,
        ),
    ),
)
def test_database_disabled_blocks_every_remaining_postgres_read_before_engine(
    monkeypatch, module_name, path, engine_name, detail, is_admin
):
    from dashboard.app import main as dashboard_main
    from dashboard.app.feature_flags import get_feature_flags
    from dashboard.app.routers import intelligence, operations
    from dashboard.app.security import CRMPrincipal, require_crm_principal

    _clear_flags(monkeypatch)
    get_feature_flags.cache_clear()
    module = {"intelligence": intelligence, "operations": operations}[module_name]
    opened = False

    def forbidden_engine():
        nonlocal opened
        opened = True
        raise AssertionError("disabled CRM database must not be opened")

    async def principal_override():
        return CRMPrincipal(
            workspace_id=uuid4(), subject="cutover-test", is_admin=is_admin
        )

    monkeypatch.setattr(module, engine_name, forbidden_engine)
    dashboard_main.app.dependency_overrides[require_crm_principal] = principal_override
    try:
        response = TestClient(dashboard_main.app).get(path)
    finally:
        dashboard_main.app.dependency_overrides.pop(require_crm_principal, None)
        get_feature_flags.cache_clear()

    assert response.status_code == 503
    assert response.json() == {"detail": detail}
    assert opened is False


def test_postgres_writer_disables_legacy_sheet_mutations(monkeypatch):
    from dashboard.app import main as dashboard_main
    from dashboard.app.config import get_settings
    from dashboard.app.feature_flags import get_feature_flags

    class ForbiddenSheet:
        touched = False

        def _refresh_cache(self):
            self.touched = True
            raise AssertionError("legacy Sheet writer must be disabled")

    _clear_flags(monkeypatch)
    monkeypatch.setenv("CRM_DB_ENABLED", "true")
    monkeypatch.setenv("CRM_COMMAND_WRITER", "postgres")
    monkeypatch.setenv("CRM_WRITE_TOKEN", "write-token")
    monkeypatch.setenv("CRM_CSRF_TOKEN", "csrf-token")
    monkeypatch.setenv("CRM_ENV", "test")
    get_settings.cache_clear()
    get_feature_flags.cache_clear()
    sheet = ForbiddenSheet()
    monkeypatch.setattr(dashboard_main, "crm", sheet)

    response = TestClient(dashboard_main.app).post(
        "/api/refresh",
        headers={
            "Authorization": "Bearer write-token",
            "X-CSRF-Token": "csrf-token",
        },
    )

    get_settings.cache_clear()
    get_feature_flags.cache_clear()
    assert response.status_code == 503
    assert response.json() == {"detail": "Writer unavailable"}
    assert sheet.touched is False
