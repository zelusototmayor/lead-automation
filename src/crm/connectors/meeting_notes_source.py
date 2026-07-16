"""Scoped read-only meeting-note connector."""

from __future__ import annotations

from src.crm.connectors.sheets_source import (
    ConnectorPage,
    PageTransport,
    _EnabledSource,
    _page,
    _scoped_event_key,
)


class MeetingNotesSource(_EnabledSource):
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
            try:
                raw = self._transport.fetch(scope, cursor)
            except (TimeoutError, ConnectionError):
                raw = self._transport.fetch(scope, cursor)
            return _page(
                raw,
                system="granola",
                scope=scope,
                event_type="meeting.note.observed",
                key=lambda item: _scoped_event_key("granola", scope, item["id"]),
                allowed_fact_names=frozenset(
                    {
                        "id",
                        "occurred_at",
                        "meeting_external_id",
                        "classification",
                        "has_notes",
                        "contact_email",
                        "domain",
                        "company_name",
                        "sector",
                        "commercial_vertical",
                    }
                ),
            )
        except Exception:
            raise RuntimeError("connector fetch failed") from None
