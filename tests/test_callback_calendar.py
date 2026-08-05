from __future__ import annotations

from src.crm.callback_calendar import CallbackCalendar


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_delete_event_preserves_non_callback_calendar_event(monkeypatch):
    calendar = CallbackCalendar("unused.json", calendar_id="primary")
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path))
        if method == "GET":
            return FakeResponse(
                200,
                {
                    "id": "external-meeting",
                    "summary": "Agentes de IA - Teixeira Trans",
                    "description": "Client meeting with attendees",
                },
            )
        return FakeResponse(204)

    monkeypatch.setattr(calendar, "_request", fake_request)

    result = calendar.delete_event("external-meeting")

    assert calls == [("GET", "/calendars/primary/events/external-meeting")]
    assert result.ok is True
    assert result.event_id == ""
    assert "not a PT Logistics callback" in result.warning


def test_delete_event_preserves_untagged_legacy_looking_calendar_event(monkeypatch):
    calendar = CallbackCalendar("unused.json", calendar_id="primary")
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path))
        if method == "GET":
            return FakeResponse(
                200,
                {
                    "id": "untagged-legacy-callback",
                    "summary": "Call: Teixeira Trans",
                    "description": "Created from the PT Logistics dashboard callback workflow.",
                },
            )
        return FakeResponse(204)

    monkeypatch.setattr(calendar, "_request", fake_request)

    result = calendar.delete_event("untagged-legacy-callback")

    assert calls == [("GET", "/calendars/primary/events/untagged-legacy-callback")]
    assert result.ok is True
    assert result.event_id == ""
    assert "not a PT Logistics callback" in result.warning


def test_upsert_event_creates_callback_instead_of_patching_external_event(monkeypatch):
    calendar = CallbackCalendar("unused.json", calendar_id="primary")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return FakeResponse(
                200,
                {
                    "id": "external-meeting",
                    "summary": "Agentes de IA - Teixeira Trans",
                    "description": "Client meeting with attendees",
                },
            )
        if method == "POST":
            return FakeResponse(200, {"id": "new-callback"})
        return FakeResponse(200, {"id": "external-meeting"})

    monkeypatch.setattr(calendar, "_request", fake_request)

    result = calendar.upsert_event(
        event_id="external-meeting",
        due_date="2026-08-11",
        due_time="15:00",
        title="Call: Teixeira Trans",
        description="Follow up",
    )

    assert [method for method, _, _ in calls] == ["GET", "POST"]
    assert calls[1][2]["json"]["extendedProperties"]["private"] == {
        "pt_logistics_callback": "1"
    }
    assert result.ok is True
    assert result.event_id == "new-callback"


def test_upsert_event_creates_callback_instead_of_patching_untagged_legacy_looking_event(monkeypatch):
    calendar = CallbackCalendar("unused.json", calendar_id="primary")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return FakeResponse(
                200,
                {
                    "id": "untagged-legacy-callback",
                    "summary": "Call: Teixeira Trans",
                    "description": "Created from the PT Logistics dashboard callback workflow.",
                },
            )
        if method == "POST":
            return FakeResponse(200, {"id": "new-callback"})
        return FakeResponse(200, {"id": "untagged-legacy-callback"})

    monkeypatch.setattr(calendar, "_request", fake_request)

    result = calendar.upsert_event(
        event_id="untagged-legacy-callback",
        due_date="2026-08-11",
        due_time="15:00",
        title="Call: Teixeira Trans",
        description="Follow up",
    )

    assert [method for method, _, _ in calls] == ["GET", "POST"]
    assert calls[1][2]["json"]["extendedProperties"]["private"] == {
        "pt_logistics_callback": "1"
    }
    assert result.ok is True
    assert result.event_id == "new-callback"


def test_upsert_event_still_patches_tagged_callback(monkeypatch):
    calendar = CallbackCalendar("unused.json", calendar_id="primary")
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return FakeResponse(
                200,
                {
                    "id": "owned-callback",
                    "extendedProperties": {
                        "private": {"pt_logistics_callback": "1"}
                    },
                },
            )
        return FakeResponse(200, {"id": "owned-callback"})

    monkeypatch.setattr(calendar, "_request", fake_request)

    result = calendar.upsert_event(
        event_id="owned-callback",
        due_date="2026-08-11",
        due_time="15:00",
        title="Call: Teixeira Trans",
        description="Follow up",
    )

    assert [method for method, _, _ in calls] == ["GET", "PATCH"]
    assert calls[1][2]["json"]["extendedProperties"]["private"] == {
        "pt_logistics_callback": "1"
    }
    assert result.ok is True
    assert result.event_id == "owned-callback"


def test_delete_event_still_deletes_owned_callback(monkeypatch):
    calendar = CallbackCalendar("unused.json", calendar_id="primary")
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path))
        if method == "GET":
            return FakeResponse(
                200,
                {
                    "id": "owned-callback",
                    "extendedProperties": {
                        "private": {"pt_logistics_callback": "1"}
                    },
                },
            )
        return FakeResponse(204)

    monkeypatch.setattr(calendar, "_request", fake_request)

    result = calendar.delete_event("owned-callback")

    assert [method for method, _ in calls] == ["GET", "DELETE"]
    assert result.ok is True
    assert result.event_id == ""
    assert result.warning == ""
