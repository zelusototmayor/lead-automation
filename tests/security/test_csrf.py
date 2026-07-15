from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer write-token",
        "X-CSRF-Token": "csrf-token",
    }


def _configure(monkeypatch, *, origins: str = "https://crm.example.test", environment: str = "test"):
    monkeypatch.setenv("CRM_WRITE_TOKEN", "write-token")
    monkeypatch.setenv("CRM_CSRF_TOKEN", "csrf-token")
    monkeypatch.setenv("CRM_ALLOWED_WRITE_ORIGINS", origins)
    monkeypatch.setenv("CRM_ENV", environment)
    get_settings.cache_clear()


def test_disallowed_origin_is_forbidden_before_route_body(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(dashboard_main, "crm", None)

    response = TestClient(dashboard_main.app).post(
        "/api/refresh",
        headers={**_headers(), "Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_missing_server_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("CRM_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("CRM_CSRF_TOKEN", raising=False)
    monkeypatch.delenv("CRM_ALLOWED_WRITE_ORIGINS", raising=False)
    get_settings.cache_clear()

    response = TestClient(dashboard_main.app).post("/api/refresh", headers=_headers())

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


@pytest.mark.parametrize(
    ("origins", "environment"),
    (
        ("http://crm.example.test", "production"),
        ("https://crm.example.test/path", "production"),
        ("https://crm.example.test?query=yes", "production"),
        ("https://crm.example.test/", "production"),
        ("https://crm.example.test#", "production"),
        ("https://crm.example.test:bad", "production"),
        ("https://:443", "production"),
        ("https://bad host.example", "production"),
        ("https://bad..example", "production"),
        ("https://-bad.example", "production"),
        ("https://999.999.999.999", "production"),
        ("https://[not-an-ip]", "production"),
        ("https://bad\thost.example", "production"),
        ("not-an-origin", "production"),
        ("http://localhost:8000", "production"),
    ),
)
def test_settings_reject_malformed_or_unsafe_origins(monkeypatch, origins, environment):
    _configure(monkeypatch, origins=origins, environment=environment)

    with pytest.raises(ValueError, match="CRM_ALLOWED_WRITE_ORIGINS"):
        get_settings()


@pytest.mark.parametrize(
    "origin",
    ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"),
)
def test_localhost_http_origin_is_allowed_only_in_explicit_test_configuration(
    monkeypatch, origin
):
    _configure(monkeypatch, origins=origin, environment="test")

    settings = get_settings()

    assert settings.allowed_write_origins == (origin,)


def test_malformed_origin_configuration_returns_generic_forbidden(monkeypatch):
    _configure(monkeypatch, origins="https://:443", environment="production")
    monkeypatch.setattr(dashboard_main, "crm", None)

    response = TestClient(dashboard_main.app).post("/api/refresh", headers=_headers())

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


@pytest.mark.parametrize(
    "origin",
    (
        "https://crm.example.test",
        "https://crm.example.test:8443",
        "https://192.0.2.10:443",
        "https://[2001:db8::1]:8443",
        "https://bücher.example",
    ),
)
def test_settings_preserve_valid_https_origins(monkeypatch, origin):
    _configure(monkeypatch, origins=origin, environment="production")

    assert get_settings().allowed_write_origins == (origin,)
