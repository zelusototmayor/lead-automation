"""Scoped read-only Gmail connector."""

from __future__ import annotations

from src.crm.connectors.sheets_source import (
    ConnectorPage,
    PageTransport,
    _EnabledSource,
    _page,
    _scoped_event_key,
)


class CursorExpiredError(RuntimeError):
    """Transport signal that the opaque incremental cursor is no longer usable."""


class GmailSource(_EnabledSource):
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
            except CursorExpiredError:
                raw = self._transport.fetch(scope, None)
            return _page(
                raw,
                system="gmail",
                scope=scope,
                event_type="gmail.message.observed",
                key=lambda item: _scoped_event_key("gmail", scope, item["id"]),
                allowed_fact_names=frozenset(
                    {
                        "id",
                        "thread_id",
                        "occurred_at",
                        "direction",
                        "classification",
                        "has_attachments",
                        "attachment_name",
                        "attachment_content_hash",
                        "currency",
                        "one_off_amount",
                        "mrr_amount",
                        "arr_amount",
                        "value_ambiguous",
                        "contact_email",
                        "domain",
                        "company_name",
                    }
                ),
            )
        except Exception:
            raise RuntimeError("connector fetch failed") from None
