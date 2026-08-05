from __future__ import annotations

import json

import pytest


SCOPES = ["scope-a", "scope-b"]


def test_loads_authorized_user_credentials(tmp_path, monkeypatch):
    from src.crm import google_credentials

    path = tmp_path / "authorized-user.json"
    path.write_text(json.dumps({"type": "authorized_user"}))
    sentinel = object()
    calls = []

    def fake_load(credentials_file):
        calls.append(credentials_file)
        return sentinel

    monkeypatch.setattr(
        google_credentials.UserCredentials,
        "from_authorized_user_file",
        fake_load,
    )

    result = google_credentials.load_google_credentials(str(path), SCOPES)

    assert result is sentinel
    assert calls == [str(path)]


def test_loads_service_account_credentials(tmp_path, monkeypatch):
    from src.crm import google_credentials

    path = tmp_path / "service-account.json"
    path.write_text(json.dumps({"type": "service_account"}))
    sentinel = object()
    calls = []

    def fake_load(credentials_file, *, scopes):
        calls.append((credentials_file, scopes))
        return sentinel

    monkeypatch.setattr(
        google_credentials.ServiceAccountCredentials,
        "from_service_account_file",
        fake_load,
    )

    result = google_credentials.load_google_credentials(str(path), SCOPES)

    assert result is sentinel
    assert calls == [(str(path), SCOPES)]


def test_rejects_unknown_google_credential_type(tmp_path):
    from src.crm.google_credentials import load_google_credentials

    path = tmp_path / "unknown.json"
    path.write_text(json.dumps({"type": "external_account"}))

    with pytest.raises(ValueError, match="Unsupported Google credential type"):
        load_google_credentials(str(path), SCOPES)
