from __future__ import annotations

from unittest.mock import MagicMock


def test_pt_logistics_uses_separate_calendar_credentials(monkeypatch):
    from src.crm import pt_logistics_sheet

    calendar_calls = []
    sheets_calls = []

    class FakeCalendar:
        def __init__(self, **kwargs):
            calendar_calls.append(kwargs)

    fake_spreadsheet = MagicMock()
    fake_spreadsheet.worksheet.side_effect = lambda name: MagicMock(title=name)
    fake_client = MagicMock()
    fake_client.open_by_key.return_value = fake_spreadsheet

    monkeypatch.setattr(pt_logistics_sheet, "CallbackCalendar", FakeCalendar)
    monkeypatch.setattr(
        pt_logistics_sheet,
        "load_google_credentials",
        lambda path, scopes: sheets_calls.append((path, scopes)) or object(),
    )
    monkeypatch.setattr(pt_logistics_sheet.gspread, "authorize", lambda _creds: fake_client)
    monkeypatch.setattr(pt_logistics_sheet.PTLogisticsCRM, "_ensure_headers", lambda self: None)
    monkeypatch.setattr(pt_logistics_sheet.PTLogisticsCRM, "_ensure_activity_headers", lambda self: None)
    monkeypatch.setattr(pt_logistics_sheet.PTLogisticsCRM, "_ensure_stage_event_headers", lambda self: None)
    monkeypatch.setattr(pt_logistics_sheet.PTLogisticsCRM, "_refresh_cache", lambda self: None)

    pt_logistics_sheet.PTLogisticsCRM(
        credentials_file="sheets.json",
        callback_credentials_file="calendar.json",
        spreadsheet_id="sheet-id",
        callback_calendar_id="zelu@example.com",
    )

    assert sheets_calls[0][0] == "sheets.json"
    assert calendar_calls == [
        {
            "credentials_file": "calendar.json",
            "calendar_id": "zelu@example.com",
            "timezone": "Europe/Lisbon",
        }
    ]


def test_dashboard_passes_separate_calendar_credentials(monkeypatch):
    import asyncio
    import importlib

    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", "sheets.json")
    monkeypatch.setenv("CALLBACK_GOOGLE_CREDENTIALS_FILE", "calendar.json")

    from dashboard.app import main

    main = importlib.reload(main)
    calls = []

    class FakeCRM:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(main, "PTLogisticsCRM", FakeCRM)

    async def exercise_lifespan():
        async with main.lifespan(main.app):
            pass

    asyncio.run(exercise_lifespan())

    assert calls[0]["credentials_file"] == "sheets.json"
    assert calls[0]["callback_credentials_file"] == "calendar.json"
