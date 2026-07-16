"""Scoped read-only Gmail connector."""

from __future__ import annotations

from src.crm.connectors.sheets_source import ConnectorPage, PageTransport, _EnabledSource, _page


class CursorExpiredError(RuntimeError):
    """Transport signal that the opaque incremental cursor is no longer usable."""


class GmailSource(_EnabledSource):
    def __init__(self, *, transport: PageTransport, enabled: bool = False, allowed_scopes=frozenset()):
        super().__init__(transport=transport, enabled=enabled, allowed_scopes=allowed_scopes)

    def fetch_page(self, scope: str, cursor: str | None) -> ConnectorPage:
        self._authorize(scope)
        try:
            try:
                raw = self._transport.fetch(scope, cursor)
            except CursorExpiredError:
                raw = self._transport.fetch(scope, None)
            return _page(
                raw,
                system="gmail",
                scope=scope,
                event_type="gmail.message.observed",
                key=lambda item: f"gmail:{item['id']}",
            )
        except Exception:
            raise RuntimeError("connector fetch failed") from None
