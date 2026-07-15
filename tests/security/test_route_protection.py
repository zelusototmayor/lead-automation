from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings


WRITE_TOKEN = "write-token-for-tests"
CSRF_TOKEN = "csrf-token-for-tests"
WRITE_PATHS = (
    "/api/log-call",
    "/api/update-lead",
    "/api/mark-email-followup",
    "/api/mark-proposal-followup",
    "/api/update-proposal",
    "/api/mark-email-sent",
    "/api/refresh",
)


def _raw_asgi_post(path: str, headers: list[tuple[bytes, bytes]]) -> tuple[int, dict, dict]:
    """Send raw header bytes through the full ASGI app, bypassing client encoding."""
    messages: list[dict] = []
    request_messages = iter(
        (
            {"type": "http.request", "body": b"{}", "more_body": False},
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
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"), *headers],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(dashboard_main.app(scope, receive, send))

    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1") for key, value in start["headers"]
    }
    return start["status"], json.loads(body), response_headers


class FakeCRM:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._cache = [["row"]]

    def _record(self, name: str) -> bool:
        self.calls.append(name)
        return True

    def log_call(self, **kwargs):
        return self._record("log_call")

    def update_lead(self, **kwargs):
        return self._record("update_lead")

    def mark_email_followup_sent(self, **kwargs):
        return self._record("mark_email_followup_sent")

    def mark_proposal_followup_sent(self, **kwargs):
        return self._record("mark_proposal_followup_sent")

    def update_proposal(self, **kwargs):
        return self._record("update_proposal")

    def mark_manual_email_sent(self, **kwargs):
        return self._record("mark_manual_email_sent")

    def _refresh_cache(self):
        self.calls.append("refresh")

    def consume_warning(self):
        return ""


@pytest.fixture(autouse=True)
def configured_security(monkeypatch):
    monkeypatch.setenv("CRM_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("CRM_CSRF_TOKEN", CSRF_TOKEN)
    monkeypatch.setenv("CRM_ALLOWED_WRITE_ORIGINS", "https://crm.example.test")
    monkeypatch.setenv("CRM_ENV", "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("path", WRITE_PATHS)
@pytest.mark.parametrize(
    "headers",
    (
        {},
        {"Authorization": f"Bearer {WRITE_TOKEN}"},
        {"X-CSRF-Token": CSRF_TOKEN},
        {"Authorization": "Bearer wrong", "X-CSRF-Token": CSRF_TOKEN},
        {"Authorization": f"Bearer {WRITE_TOKEN}", "X-CSRF-Token": "wrong"},
    ),
)
def test_every_human_write_fails_closed_before_crm_access(monkeypatch, path, headers):
    class ExplodingCRM:
        def __getattr__(self, name):
            raise AssertionError(f"CRM was accessed via {name}")

    monkeypatch.setattr(dashboard_main, "crm", ExplodingCRM())
    response = TestClient(dashboard_main.app).post(path, headers=headers, json={})

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "headers",
    (
        [(b"authorization", b"Bearer \xff"), (b"x-csrf-token", CSRF_TOKEN.encode("ascii"))],
        [(b"authorization", f"Bearer {WRITE_TOKEN}".encode("ascii")), (b"x-csrf-token", b"\xff")],
    ),
)
def test_non_ascii_credential_bytes_return_generic_forbidden_with_security_headers(headers):
    status_code, body, response_headers = _raw_asgi_post("/api/refresh", headers)

    assert status_code == 403
    assert body == {"detail": "Forbidden"}
    assert "www-authenticate" not in response_headers
    assert response_headers["x-content-type-options"] == "nosniff"
    assert response_headers["x-frame-options"] == "DENY"
    assert response_headers["content-security-policy"]


@pytest.mark.parametrize(
    ("path", "body", "expected_call"),
    (
        ("/api/log-call", {"lead_id": "lead-1", "call_status": "Connected"}, "log_call"),
        ("/api/update-lead", {"lead_id": "lead-1", "updates": {"stage": "Called"}}, "update_lead"),
        ("/api/mark-email-followup", {"lead_id": "lead-1", "task_type": "Follow-up"}, "mark_email_followup_sent"),
        ("/api/mark-proposal-followup", {"lead_id": "lead-1", "task_type": "Proposal"}, "mark_proposal_followup_sent"),
        ("/api/update-proposal", {"lead_id": "lead-1", "status": "Sent"}, "update_proposal"),
        ("/api/mark-email-sent", {"lead_id": "lead-1"}, "mark_manual_email_sent"),
        ("/api/refresh", {}, "refresh"),
    ),
)
@pytest.mark.parametrize("origin", (None, "https://crm.example.test"))
def test_valid_server_credentials_reach_each_write_route(monkeypatch, path, body, expected_call, origin):
    fake = FakeCRM()
    monkeypatch.setattr(dashboard_main, "crm", fake)
    headers = {
        "Authorization": f"Bearer {WRITE_TOKEN}",
        "X-CSRF-Token": CSRF_TOKEN,
    }
    if origin:
        headers["Origin"] = origin

    response = TestClient(dashboard_main.app).post(path, headers=headers, json=body)

    assert response.status_code == 200
    assert expected_call in fake.calls


def test_rich_get_api_remains_public_without_auth_challenge(monkeypatch):
    monkeypatch.setattr(dashboard_main, "crm", None)

    response = TestClient(dashboard_main.app).get("/api/account-profiles")

    assert response.status_code == 503
    assert "www-authenticate" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_health_check_is_public_and_minimal():
    response = TestClient(dashboard_main.app).get("/up")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_responses_include_browser_security_headers():
    response = TestClient(dashboard_main.app).get("/up")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"]
    assert response.headers["permissions-policy"]
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "fonts.googleapis.com" in csp
    assert "cdn.jsdelivr.net" in csp
    assert "'unsafe-inline'" in csp


def test_dashboard_warns_that_public_ui_is_read_only_without_exposing_tokens():
    response = TestClient(dashboard_main.app).get("/")

    assert response.status_code == 200
    assert "interface pública é apenas de leitura" in response.text.lower()
    assert "canal autenticado no servidor" in response.text.lower()
    assert WRITE_TOKEN not in response.text
    assert CSRF_TOKEN not in response.text


def test_public_dashboard_disables_every_write_trigger():
    response = TestClient(dashboard_main.app).get("/")

    assert response.status_code == 200
    assert "publicReadOnly: true" in response.text
    for action in (
        "startDetailEdit()",
        "saveDetailUpdate()",
        "saveQuickUpdate()",
        "markEmailSent()",
        "saveProposalUpdate()",
        "logCall()",
    ):
        buttons = re.findall(
            rf'<button[^>]*@click="{re.escape(action)}"[^>]*>', response.text
        )
        assert buttons, f"missing write control for {action}"
        assert all("publicReadOnly" in button and ":disabled=" in button for button in buttons)


def test_public_dashboard_reload_is_get_only_for_manual_and_hourly_refresh():
    response = TestClient(dashboard_main.app).get("/")

    assert response.status_code == 200
    assert '@click="reloadView()"' in response.text
    assert "Reload view" in response.text
    assert "async reloadView(silent = false)" in response.text
    assert "setInterval(() => this.reloadView(true), 60 * 60 * 1000)" in response.text
    assert "await this.loadStats()" in response.text
    assert "await this.loadHistory()" in response.text
    assert "await this.loadItems()" in response.text
    assert "await this.loadCRMIntelligence()" in response.text
    assert "fetch('/api/refresh', { method: 'POST' })" not in response.text
    assert '@click="refresh()"' not in response.text
