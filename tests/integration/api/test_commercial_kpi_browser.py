"""Real Chromium on the real leads page and disposable PostgreSQL/API only."""

import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from tests.integration.api import test_lead_operations_api as _api_fixtures
from tests.integration.api.test_commercial_kpi_service import proposal, source

lead_operations_api = _api_fixtures.lead_operations_api


@pytest.fixture
def browser_api(lead_operations_api, monkeypatch):
    import uvicorn

    from dashboard.app.config import get_settings
    from dashboard.app.main import app
    from dashboard.app.routers import accounts

    _client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    monkeypatch.setenv("CRM_ALLOWED_WRITE_ORIGINS", url)
    get_settings.cache_clear()
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="error", access_log=False, lifespan="off", ws="none"
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield url, engine, workspace, lead, actor
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_real_leads_correction_readback_and_note_save_invalidates_before_worker(
    browser_api, width, height
):
    from playwright.sync_api import expect, sync_playwright

    from src.crm.persistence.models import Activity, Lead
    from src.crm.services.commercial_kpi_service import SourceIndex, assess
    from src.crm.services.note_source_service import source_digest

    url, engine, workspace, lead, actor = browser_api
    with Session(engine) as session, session.begin():
        lead_row = session.get(Lead, lead)
        call = source(
            session,
            workspace,
            lead,
            account_id=lead_row.account_id,
            contact_id=lead_row.contact_id,
            occurred_at=datetime.now(UTC),
            summary="Responsável discutiu necessidade e recusou por orçamento.",
        )
        call_id = call.id
        original = source_digest(call)
        context = SourceIndex(session, workspace).context(call_id)
        assess(
            session,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=context["source_digest"],
            expected_context_digest=context["context_digest"],
            proposal=proposal(context),
        )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # No external integrations, trackers or fonts are needed by this test.
        page.route(
            "**/*",
            lambda route: (
                route.continue_()
                if route.request.url.startswith(url + "/")
                else route.abort()
            ),
        )
        try:
            page.goto(url + "/leads")
            panel = page.locator("[data-commercial-kpis]")
            expect(panel.locator("[data-kpi-confirmed]")).to_have_text("1")
            expect(panel.locator("[data-kpi-goal]")).to_contain_text("1/50")
            panel.get_by_role("button", name="Ver fontes").click()
            panel.get_by_role("button", name="Abrir fonte").first.click()
            dialog = page.get_by_role("dialog", name="Classificação comercial")
            expect(dialog.locator("pre")).to_contain_text("recusou por orçamento")
            assert dialog.get_by_label("Relevância", exact=True).count() == 1, (
                page.locator("#leads-app").get_attribute("data-can-add-note"),
                dialog.aria_snapshot(),
            )
            dialog.get_by_label("Relevância", exact=True).select_option("no")
            dialog.get_by_label("Justificação", exact=True).fill(
                "Correção humana sintética para validação."
            )
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/correction")
                    and response.request.method == "POST"
                )
            ) as response:
                dialog.get_by_role("button", name="Guardar correção").click()
            assert response.value.status == 201
            expect(dialog.locator("[data-kpi-provenance]")).to_have_text(
                "Humano · current"
            )
            expect(panel.locator("[data-kpi-confirmed]")).to_have_text("0")
            expect(panel.locator("[data-kpi-no]")).to_have_text("1")
            assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
            assert dialog.evaluate(
                "el => Math.abs(el.getBoundingClientRect().left + el.getBoundingClientRect().width / 2 - innerWidth / 2) < 2"
            )
            evidence = Path(
                os.environ.get(
                    "CRM_KPI_EVIDENCE",
                    str(Path(__file__).resolve().parents[4] / "evidence"),
                )
            )
            evidence.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(evidence / f"kpi-browser-{width}-human.png"), full_page=True
            )
            page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
            # Use the existing form, not a test-only invalidation event.
            if page.locator("[data-exit-focus]").is_visible():
                page.locator("[data-exit-focus]").click()
            page.locator('[data-pipeline-queue="all"]').click()
            page.locator(".lead-row[data-lead-id]").first.click()
            note = page.locator("[data-note-form]")
            for ancestor in note.locator("xpath=ancestor::details").all():
                if not ancestor.get_attribute("open"):
                    ancestor.locator("summary").first.click()
            note.locator('textarea[name="summary"]').fill(
                "Contexto posterior: a interpretação da conversa necessita de revisão."
            )
            note.get_by_role("button", name="Adicionar nota", exact=True).click()
            expect(panel.locator("[data-kpi-stale]")).to_have_text("1")
            expect(panel.locator("[data-kpi-unknown]")).to_have_text("1")
            assert panel.evaluate(
                "el => el.getBoundingClientRect().width <= innerWidth"
            )
            page.screenshot(
                path=str(evidence / f"kpi-browser-{width}-stale.png"), full_page=True
            )
            assert not errors, errors
            with Session(engine) as session:
                assert source_digest(session.get(Activity, call_id)) == original
        finally:
            browser.close()


def test_real_browser_public_error_loading_unknown_and_no_new_detail_access(
    browser_api,
):
    from playwright.sync_api import expect, sync_playwright

    from dashboard.app.main import app
    from dashboard.app.security import CRMPrincipal, require_crm_principal
    from src.crm.persistence.models import Lead

    url, engine, workspace, lead, actor = browser_api
    with Session(engine) as session, session.begin():
        row = session.get(Lead, lead)
        call = source(
            session,
            workspace,
            lead,
            account_id=row.account_id,
            contact_id=row.contact_id,
            occurred_at=datetime.now(UTC),
            summary="SYNTHETIC_PRIVATE_SOURCE <img src=x onerror=alert(1)>",
        )
        call_id = call.id
    app.dependency_overrides[require_crm_principal] = lambda: CRMPrincipal(
        workspace_id=workspace,
        actor_id=actor,
        subject="public-browser",
        permissions=frozenset({"crm:read", "crm:note:write"}),
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            pattern = "**/api/v1/pipeline/call-metrics?*"
            page.route(
                pattern, lambda route: route.fulfill(status=503, body="Unavailable")
            )
            page.goto(url + "/leads")
            panel = page.locator("[data-commercial-kpis]")
            expect(panel).to_contain_text("Não foi possível atualizar")
            expect(panel.locator("[data-kpi-confirmed]")).to_have_count(0)
            page.unroute(pattern)
            pending = []
            page.route(pattern, lambda route: pending.append(route))
            panel.get_by_role("button", name="Atualizar KPIs").click()
            expect(panel).to_contain_text("A atualizar")
            expect(panel.locator("[data-kpi-confirmed]")).to_have_count(0)
            assert len(pending) == 1
            pending[0].continue_()
            expect(panel.locator("[data-kpi-unknown]")).to_have_text("1")
            expect(panel.locator("[data-kpi-pending]")).to_have_text("1")
            expect(panel.get_by_role("button", name="Ver fontes")).to_have_count(0)
            assert "SYNTHETIC_PRIVATE_SOURCE" not in panel.inner_text()
            assert str(call_id) not in panel.inner_html()
            assert (
                page.request.get(
                    url + f"/api/v1/pipeline/call-metrics/activities/{call_id}"
                ).status
                == 401
            )
            assert (
                page.request.post(
                    url
                    + f"/api/v1/pipeline/call-metrics/activities/{call_id}/correction",
                    data={},
                ).status
                == 401
            )
        finally:
            browser.close()
