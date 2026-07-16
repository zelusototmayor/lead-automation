"""Scoped read-only Google Calendar connector."""

from __future__ import annotations

from src.crm.connectors.sheets_source import (
    ConnectorPage,
    PageTransport,
    _EnabledSource,
    _page,
    _scoped_event_key,
)


class CalendarSource(_EnabledSource):
    def __init__(
        self,
        *,
        transport: PageTransport,
        enabled: bool = False,
        allowed_scopes=frozenset(),
    ):
        super().__init__(
            transport=transport, enabled=enabled, allowed_scopes=allowed_scopes
        )

    def fetch_page(self, scope: str, cursor: str | None) -> ConnectorPage:
        self._authorize(scope)
        try:
            raw = self._transport.fetch(scope, cursor)
            return _page(
                raw,
                system="google_calendar",
                scope=scope,
                event_type="calendar.meeting.observed",
                key=lambda item: _scoped_event_key(
                    "google_calendar", scope, item["id"], item["updated"]
                ),
                allowed_fact_names=frozenset(
                    {
                        "id",
                        "updated",
                        "occurred_at",
                        "classification",
                        "status",
                        "contact_email",
                        "domain",
                        "company_name",
                    }
                ),
            )
        except Exception:
            raise RuntimeError("connector fetch failed") from None
