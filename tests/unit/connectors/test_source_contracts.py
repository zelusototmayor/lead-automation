from datetime import UTC, datetime

import pytest

from src.crm.connectors.calendar_source import CalendarSource
from src.crm.connectors.gmail_source import CursorExpiredError, GmailSource
from src.crm.connectors.meeting_notes_source import MeetingNotesSource
from src.crm.connectors.sheets_source import ConnectorDisabledError, GoogleSheetsSource


NOW = datetime(2026, 7, 16, tzinfo=UTC)


class FakeTransport:
    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error
        self.calls = []

    def fetch(self, scope, cursor):
        self.calls.append((scope, cursor))
        if self.error:
            error, self.error = self.error, None
            raise error
        return self.pages.pop(0)


def test_sources_are_disabled_by_default_and_require_allowlisted_scope():
    source = GoogleSheetsSource(transport=FakeTransport())
    with pytest.raises(ConnectorDisabledError, match="connector unavailable"):
        source.fetch_page("sheet:allowed", None)

    source = GoogleSheetsSource(
        transport=FakeTransport(), enabled=True, allowed_scopes={"sheet:allowed"}
    )
    with pytest.raises(ConnectorDisabledError, match="connector unavailable"):
        source.fetch_page("sheet:other", None)


def test_gmail_recovers_once_from_expired_cursor_without_leaking_cursor():
    transport = FakeTransport(
        pages=[
            {
                "items": [{"id": "m1", "thread_id": "t1", "occurred_at": NOW}],
                "next_cursor": "fresh",
            }
        ],
        error=CursorExpiredError("secret old cursor"),
    )
    source = GmailSource(
        transport=transport, enabled=True, allowed_scopes={"mailbox:one"}
    )

    page = source.fetch_page("mailbox:one", "expired-secret")

    assert page.next_cursor == "fresh"
    assert [event.idempotency_key for event in page.events] == ["gmail:m1"]
    assert transport.calls == [("mailbox:one", "expired-secret"), ("mailbox:one", None)]


def test_calendar_reschedule_has_revision_key_and_preserves_occurrence_time():
    transport = FakeTransport(
        pages=[
            {
                "items": [
                    {
                        "id": "cal-1",
                        "updated": "2026-07-16T11:00:00+00:00",
                        "occurred_at": "2026-07-20T10:00:00+00:00",
                    }
                ],
                "next_cursor": "next",
            }
        ]
    )
    source = CalendarSource(
        transport=transport, enabled=True, allowed_scopes={"calendar:commercial"}
    )

    event = source.fetch_page("calendar:commercial", None).events[0]

    assert event.idempotency_key == "google_calendar:cal-1:2026-07-16T11:00:00+00:00"
    assert event.envelope.occurred_at == datetime(2026, 7, 20, 10, tzinfo=UTC)


def test_meeting_notes_retries_one_transient_failure_and_returns_generic_failure():
    transport = FakeTransport(
        pages=[
            {
                "items": [{"id": "note-1", "occurred_at": NOW}],
                "next_cursor": "next",
            }
        ],
        error=TimeoutError("secret token"),
    )
    source = MeetingNotesSource(
        transport=transport, enabled=True, allowed_scopes={"granola:team"}
    )
    assert source.fetch_page("granola:team", None).events[0].idempotency_key == "granola:note-1"

    failing = MeetingNotesSource(
        transport=FakeTransport(error=TimeoutError("secret token")),
        enabled=True,
        allowed_scopes={"granola:team"},
    )
    with pytest.raises(RuntimeError, match="connector fetch failed") as exc:
        failing.fetch_page("granola:team", None)
    assert "secret" not in str(exc.value)
