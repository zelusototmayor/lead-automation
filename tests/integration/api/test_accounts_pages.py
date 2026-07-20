from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main


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
    assert 'data-leads-list' in response.text
    assert 'data-lead-search' in response.text
    assert 'data-stage-filter' in response.text
    assert "/static/leads.js" in response.text
    assert "Empresa / contacto" in response.text
    assert "Estado" in response.text
    assert "Próxima ação" in response.text


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
