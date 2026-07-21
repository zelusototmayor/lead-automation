from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers.accounts import (
    AccountRequestContext,
    get_account_request_context,
)
from dashboard.app.security import CRMPrincipal


def test_accounts_pages_fail_closed_without_trusted_principal():
    client = TestClient(dashboard_main.app)

    assert client.get("/contas").status_code == 403
    assert client.get("/contas/00000000-0000-0000-0000-000000000001").status_code == 403


def test_accounts_index_page_has_loading_empty_error_and_mobile_contract(
    account_api_fixture,
):
    client, _ = account_api_fixture

    response = client.get("/contas")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'name="viewport"' in response.text
    assert 'data-state="loading"' in response.text
    assert 'data-state="empty"' in response.text
    assert 'data-state="error"' in response.text
    assert "/static/accounts.js" in response.text
    assert 'href="/propostas"' in response.text
    assert 'href="/inteligencia"' in response.text


def test_leads_page_is_a_compact_mobile_first_operational_list(account_api_fixture):
    client, _ = account_api_fixture

    response = client.get("/leads")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'name="viewport"' in response.text
    assert 'data-state="loading"' in response.text
    assert 'data-state="empty"' in response.text
    assert 'data-state="error"' in response.text
    assert "data-leads-list" in response.text
    assert "data-pipeline-summary" in response.text
    assert 'data-pipeline-queue="calls_overdue"' in response.text
    assert 'data-pipeline-queue="proposal_followups_today"' in response.text
    assert 'data-pipeline-queue="untouched"' in response.text
    assert "data-lead-detail-panel" in response.text
    assert "data-lead-timeline" in response.text
    assert "data-lead-tasks" in response.text
    assert "data-task-complete" in response.text
    assert "data-task-reschedule" in response.text
    assert "data-task-cancel" in response.text
    assert "data-lead-search" in response.text
    assert "data-stage-filter" in response.text
    assert 'data-writable="false"' in response.text
    assert "data-csrf-token" not in response.text
    assert "/static/leads.js" in response.text
    assert "Empresa / contacto" in response.text
    assert "Estado" in response.text
    assert "Próxima ação" in response.text


def test_leads_page_has_future_queues_and_dedicated_strict_priority_filter(
    account_api_fixture,
):
    client, _ = account_api_fixture

    response = client.get("/leads")
    script = (
        Path(__file__).parents[3] / "dashboard" / "app" / "static" / "leads.js"
    ).read_text(encoding="utf-8")

    assert response.status_code == 200
    assert 'data-pipeline-queue="calls_future"' in response.text
    assert 'data-pipeline-queue="emails_future"' in response.text
    assert "data-priority-filter" in response.text
    for priority in ("low", "medium", "high"):
        assert f'<option value="{priority}">' in response.text
    assert 'searchParams.set("priority", selectedPriority)' in script
    assert 'priorityFilter.addEventListener("change",' in script
    assert 'search.addEventListener("input", () =>' in script
    assert 'stageFilter.addEventListener("change", () =>' in script
    assert "queueBehavior.invalidateNavigation()" in script


def test_leads_page_exposes_save_and_next_and_skip_controls(
    account_api_fixture, monkeypatch
):
    _, ids = account_api_fixture
    for name, value in {
        "CRM_DB_ENABLED": "true",
        "CRM_ACCOUNTS_READ_MODEL": "postgres",
        "CRM_PROPOSALS_READ_MODEL": "postgres",
        "CRM_COMMAND_WRITER": "postgres",
        "CRM_SHEETS_PROJECTION_ENABLED": "false",
        "CRM_AGENT_EVENTS_ENABLED": "false",
        "CRM_CSRF_TOKEN": "fake-ui-csrf-token",
        "CRM_ALLOWED_WRITE_ORIGINS": "https://testserver",
        "CRM_ENV": "test",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    get_feature_flags.cache_clear()

    def writer_context():
        with Session(ids["engine"]) as session:
            yield AccountRequestContext(
                principal=CRMPrincipal(
                    workspace_id=ids["workspace_id"],
                    actor_id=uuid4(),
                    subject="writer",
                    permissions=frozenset({"crm:read", "crm:lead:edit"}),
                ),
                session=session,
            )

    dashboard_main.app.dependency_overrides[get_account_request_context] = (
        writer_context
    )
    try:
        response = TestClient(dashboard_main.app, base_url="https://testserver").get(
            "/leads"
        )
    finally:
        get_settings.cache_clear()
        get_feature_flags.cache_clear()
    assert response.status_code == 200
    assert "data-skip-lead" in response.text
    assert "data-advance-after-save" in response.text
    assert "Guardar e seguinte" in response.text


def test_leads_page_only_exposes_csrf_to_authorized_postgres_writer(
    account_api_fixture, monkeypatch
):
    _, ids = account_api_fixture
    for name, value in {
        "CRM_DB_ENABLED": "true",
        "CRM_ACCOUNTS_READ_MODEL": "postgres",
        "CRM_PROPOSALS_READ_MODEL": "postgres",
        "CRM_COMMAND_WRITER": "postgres",
        "CRM_SHEETS_PROJECTION_ENABLED": "false",
        "CRM_AGENT_EVENTS_ENABLED": "false",
        "CRM_CSRF_TOKEN": "fake-ui-csrf-token",
        "CRM_ALLOWED_WRITE_ORIGINS": "https://testserver",
        "CRM_ENV": "test",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    get_feature_flags.cache_clear()
    actor_id = uuid4()

    def writer_context():
        with Session(ids["engine"]) as session:
            yield AccountRequestContext(
                principal=CRMPrincipal(
                    workspace_id=ids["workspace_id"],
                    actor_id=actor_id,
                    subject="writer",
                    permissions=frozenset({"crm:read", "crm:task:write"}),
                ),
                session=session,
            )

    dashboard_main.app.dependency_overrides[get_account_request_context] = (
        writer_context
    )
    try:
        response = TestClient(dashboard_main.app, base_url="https://testserver").get(
            "/leads"
        )
    finally:
        get_settings.cache_clear()
        get_feature_flags.cache_clear()

    assert response.status_code == 200
    assert 'data-writable="true"' in response.text
    assert 'data-csrf-token="fake-ui-csrf-token"' in response.text
    assert "data-next-action-form" in response.text
    assert "data-detail-phone-link" in response.text
    assert "data-detail-email-link" in response.text
    assert "data-lead-edit-form" not in response.text
    assert "data-stage-transition-form" not in response.text
    assert "data-call-log-form" not in response.text
    assert "data-email-log-form" not in response.text
    assert "Writes canónicos ativos" in response.text


def test_leads_javascript_uses_canonical_pipeline_and_task_command_contracts():
    script = (
        Path(__file__).parents[3] / "dashboard" / "app" / "static" / "leads.js"
    ).read_text(encoding="utf-8")

    assert "/api/v1/pipeline/summary" in script
    assert "/api/v1/pipeline/items" in script
    assert "/api/v1/leads/${leadId}" in script
    assert "/api/v1/leads/${leadId}/timeline" in script
    assert "/api/v1/leads/${leadId}/tasks" in script
    assert "/api/v1/commands/tasks/${task.id}/${action}" in script
    assert "/api/v1/commands/leads/${leadId}/edit" in script
    assert "/api/v1/commands/leads/${leadId}/transition-stage" in script
    assert "/api/v1/commands/leads/${leadId}/log-call" in script
    assert "/api/v1/commands/leads/${leadId}/log-email" in script
    assert "/api/v1/commands/leads/${leadId}/schedule-next-action" in script
    assert "data-lead-edit-form" in script
    assert "data-stage-transition-form" in script
    assert "data-call-log-form" in script
    assert "data-email-log-form" in script
    assert "data-next-action-form" in script
    assert "crypto.randomUUID()" in script
    assert '"X-CSRF-Token"' in script
    assert '"Idempotency-Key"' in script


def test_account_detail_page_is_separate_and_does_not_embed_rich_data(
    account_api_fixture,
):
    client, ids = account_api_fixture

    response = client.get(f"/contas/{ids['account_id']}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'data-account-id="' + str(ids["account_id"]) + '"' in response.text
    assert "ana@example.test" not in response.text
    assert "Private meeting notes" not in response.text
    assert 'data-state="loading"' in response.text
    assert 'data-state="error"' in response.text
    assert "/static/accounts.js" in response.text


def test_account_page_cross_workspace_id_is_not_disclosed(account_api_fixture):
    client, ids = account_api_fixture

    response = client.get(f"/contas/{ids['foreign_account_id']}")

    assert response.status_code == 404
