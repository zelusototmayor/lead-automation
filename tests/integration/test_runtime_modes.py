from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.security import CRMPrincipal, require_crm_principal


@pytest.fixture(autouse=True)
def authenticated_browser_reads():
    previous_overrides = dashboard_main.app.dependency_overrides.copy()
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: (
        CRMPrincipal(
            workspace_id=UUID("11111111-2222-4333-8444-555555555555"),
            subject="runtime-test",
            is_admin=True,
        )
    )
    yield
    dashboard_main.app.dependency_overrides.clear()
    dashboard_main.app.dependency_overrides.update(previous_overrides)


_FLAG_NAMES = (
    "CRM_DB_ENABLED",
    "CRM_ACCOUNTS_READ_MODEL",
    "CRM_PROPOSALS_READ_MODEL",
    "CRM_COMMAND_WRITER",
    "CRM_SHEETS_PROJECTION_ENABLED",
    "CRM_AGENT_EVENTS_ENABLED",
)


def _set_flags(monkeypatch, **values: str) -> None:
    for name in _FLAG_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_feature_flags.cache_clear()


def test_canonical_only_startup_does_not_construct_legacy_sheet_adapter(monkeypatch):
    _set_flags(
        monkeypatch,
        CRM_DB_ENABLED="true",
        CRM_ACCOUNTS_READ_MODEL="postgres",
        CRM_PROPOSALS_READ_MODEL="postgres",
        CRM_COMMAND_WRITER="postgres",
        CRM_SHEETS_PROJECTION_ENABLED="false",
    )

    adapter_calls = 0

    def forbidden_adapter(**_kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        raise AssertionError("canonical-only startup must not touch Google Sheets")

    monkeypatch.setattr(dashboard_main, "PTLogisticsCRM", forbidden_adapter)
    dashboard_main.crm = object()

    async def run_lifespan() -> None:
        async with dashboard_main.lifespan(dashboard_main.app):
            assert dashboard_main.crm is None

    try:
        asyncio.run(run_lifespan())
        assert adapter_calls == 0
    finally:
        get_feature_flags.cache_clear()


def test_legacy_startup_defers_sheet_io_without_leaking_constructor_error(
    monkeypatch, caplog, capsys
):
    _set_flags(monkeypatch)
    marker = "startup-secret-marker"
    adapter_calls = 0

    def exploding_adapter(**_kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        raise RuntimeError(marker)

    monkeypatch.setattr(dashboard_main, "PTLogisticsCRM", exploding_adapter)
    dashboard_main.crm = object()

    async def run_lifespan() -> None:
        async with dashboard_main.lifespan(dashboard_main.app):
            assert dashboard_main.crm is None

    with caplog.at_level(logging.WARNING, logger=dashboard_main.__name__):
        asyncio.run(run_lifespan())

    captured = capsys.readouterr()
    assert adapter_calls == 0
    assert marker not in captured.out
    assert marker not in captured.err
    assert marker not in caplog.text


def test_legacy_readiness_initializes_only_the_active_sheet_dependency(monkeypatch):
    _set_flags(monkeypatch)
    sheet = object()
    adapter_calls = 0

    def adapter(**_kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        return sheet

    monkeypatch.setattr(dashboard_main, "PTLogisticsCRM", adapter)
    monkeypatch.setattr(
        dashboard_main,
        "_postgres_is_ready",
        lambda: (_ for _ in ()).throw(AssertionError("postgres readiness was checked")),
    )
    dashboard_main.crm = None

    response = TestClient(dashboard_main.app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"legacy_sheet": "ready"},
    }
    assert adapter_calls == 1


def test_lazy_legacy_adapter_initialization_is_single_flight(monkeypatch):
    _set_flags(monkeypatch)
    dashboard_main.crm = None
    workers = 4
    callers_ready = threading.Barrier(workers)
    adapters: list[object] = []
    legacy_adapter_required = dashboard_main._legacy_adapter_required

    def synchronized_legacy_adapter_required(flags):
        callers_ready.wait()
        return legacy_adapter_required(flags)

    def adapter(**_kwargs):
        instance = object()
        adapters.append(instance)
        return instance

    monkeypatch.setattr(
        dashboard_main,
        "_legacy_adapter_required",
        synchronized_legacy_adapter_required,
    )
    monkeypatch.setattr(dashboard_main, "PTLogisticsCRM", adapter)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(
            executor.map(lambda _index: dashboard_main._require_crm(), range(workers))
        )

    assert len(adapters) == 1
    assert all(result is adapters[0] for result in results)


def test_failed_lazy_constructor_does_not_clear_adapter_published_concurrently(
    monkeypatch,
):
    _set_flags(monkeypatch)
    dashboard_main.crm = None
    constructor_started = threading.Event()
    allow_failure = threading.Event()
    valid_adapter = object()

    def adapter(**_kwargs):
        constructor_started.set()
        assert allow_failure.wait(timeout=1)
        raise RuntimeError("constructor-secret-marker")

    monkeypatch.setattr(dashboard_main, "PTLogisticsCRM", adapter)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(dashboard_main._require_crm)
        assert constructor_started.wait(timeout=1)
        dashboard_main.crm = valid_adapter
        allow_failure.set()

    assert result.result() is valid_adapter
    assert dashboard_main.crm is valid_adapter


def test_authenticated_browser_fixture_restores_all_previous_dependency_overrides():
    def sentinel_dependency():
        return None

    def sentinel_override():
        return "preserved"

    previous_overrides = dashboard_main.app.dependency_overrides.copy()
    dashboard_main.app.dependency_overrides[sentinel_dependency] = sentinel_override
    fixture = authenticated_browser_reads.__wrapped__()

    try:
        next(fixture)
        dashboard_main.app.dependency_overrides[lambda: None] = lambda: "temporary"
        with pytest.raises(StopIteration):
            next(fixture)

        assert (
            dashboard_main.app.dependency_overrides[sentinel_dependency]
            is sentinel_override
        )
        assert dashboard_main.app.dependency_overrides == {
            **previous_overrides,
            sentinel_dependency: sentinel_override,
        }
    finally:
        dashboard_main.app.dependency_overrides.clear()
        dashboard_main.app.dependency_overrides.update(previous_overrides)


def test_canonical_readiness_checks_postgres_without_requiring_sheet(monkeypatch):
    _set_flags(
        monkeypatch,
        CRM_DB_ENABLED="true",
        CRM_ACCOUNTS_READ_MODEL="postgres",
        CRM_PROPOSALS_READ_MODEL="postgres",
        CRM_COMMAND_WRITER="postgres",
        CRM_SHEETS_PROJECTION_ENABLED="false",
    )
    checks = 0

    def postgres_ready() -> bool:
        nonlocal checks
        checks += 1
        return True

    monkeypatch.setattr(
        dashboard_main, "_postgres_is_ready", postgres_ready, raising=False
    )
    dashboard_main.crm = None
    try:
        response = TestClient(dashboard_main.app).get("/ready")
    finally:
        get_feature_flags.cache_clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"postgres": "ready"},
    }
    assert checks == 1


def test_postgres_readiness_disposes_its_probe_engine(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, _statement):
            return 1

    class Engine:
        disposed = False

        def connect(self):
            return Connection()

        def dispose(self):
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(dashboard_main, "create_database_engine", lambda: engine)

    assert dashboard_main._postgres_is_ready() is True
    assert engine.disposed is True


def test_account_shadow_comparison_preserves_legacy_response(monkeypatch):
    _set_flags(
        monkeypatch,
        CRM_DB_ENABLED="true",
        CRM_ACCOUNTS_READ_MODEL="shadow",
    )
    legacy_profiles = [{"company": "Legacy Co", "stage": "Meeting Booked"}]

    class LegacyCRM:
        def get_account_profiles(self, _today, *, stage):
            assert stage == "Meeting Booked"
            return legacy_profiles

    comparisons: list[object] = []
    monkeypatch.setattr(dashboard_main, "crm", LegacyCRM())
    monkeypatch.setattr(
        dashboard_main,
        "_account_shadow_comparison",
        comparisons.append,
        raising=False,
    )
    try:
        response = TestClient(dashboard_main.app).get("/api/account-profiles")
    finally:
        get_feature_flags.cache_clear()

    assert response.status_code == 200
    assert response.json() == {"profiles": legacy_profiles}
    assert comparisons == [{"profiles": legacy_profiles}]


def test_proposal_shadow_failure_is_observed_without_changing_legacy_response(
    monkeypatch, caplog
):
    _set_flags(
        monkeypatch,
        CRM_DB_ENABLED="true",
        CRM_PROPOSALS_READ_MODEL="shadow",
    )
    legacy_leads = [{"id": "legacy-1", "company": "Legacy Co"}]

    class LegacyCRM:
        def get_proposals(self, _today, *, view):
            assert view == "open"
            return legacy_leads

    def failed_comparison(_payload):
        raise RuntimeError("database-password-must-not-leak")

    monkeypatch.setattr(dashboard_main, "crm", LegacyCRM())
    monkeypatch.setattr(
        dashboard_main, "_proposal_shadow_comparison", failed_comparison
    )
    with caplog.at_level(logging.WARNING, logger=dashboard_main.__name__):
        try:
            response = TestClient(dashboard_main.app).get("/api/proposals")
        finally:
            get_feature_flags.cache_clear()

    assert response.status_code == 200
    assert response.json() == {"leads": legacy_leads, "count": 1, "view": "open"}
    assert "proposal shadow comparison failed" in caplog.text
    assert "database-password-must-not-leak" not in caplog.text
    assert "database-password-must-not-leak" not in response.text
