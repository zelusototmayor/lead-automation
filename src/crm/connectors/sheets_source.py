"""Read-only connector primitives and Google Sheets source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Protocol, Sequence

from src.crm.ingestion.checkpoints import EventToPersist
from src.crm.ingestion.contracts import EventEnvelope

READ_ONLY_SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
MAX_PAGE_ITEMS = 1000


class ConnectorDisabledError(RuntimeError):
    """A connector or requested source scope is not explicitly enabled."""


def _scoped_event_key(system: str, scope: str, *parts: object) -> str:
    """Encode provider identity without delimiter collisions across source scopes."""

    canonical = json.dumps(
        [scope, *parts], ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return f"{system}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class PageTransport(Protocol):
    def fetch(self, scope: str, cursor: str | None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ConnectorPage:
    events: tuple[EventToPersist, ...]
    next_cursor: str
    high_watermark_at: datetime | None = None


def _timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise RuntimeError("connector payload invalid") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("connector payload invalid")
    return value.astimezone(UTC)


def _page(
    raw: object,
    *,
    system: str,
    scope: str,
    event_type: str,
    key,
    allowed_fact_names: frozenset[str],
) -> ConnectorPage:
    if not isinstance(raw, dict):
        raise RuntimeError("connector payload invalid")
    items = raw.get("items")
    cursor = raw.get("next_cursor")
    if not isinstance(items, list) or len(items) > MAX_PAGE_ITEMS:
        raise RuntimeError("connector payload invalid")
    if not isinstance(cursor, str) or not cursor or len(cursor) > 65_536:
        raise RuntimeError("connector payload invalid")
    events = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise RuntimeError("connector payload invalid")
        external_id = item["id"]
        if not external_id or len(external_id) > 512:
            raise RuntimeError("connector payload invalid")
        occurred_at = _timestamp(item.get("occurred_at"))
        facts = {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in item.items()
            if name in allowed_fact_names
        }
        envelope = EventEnvelope.model_validate(
            {
                "schema_version": 1,
                "event_type": event_type,
                "source": {
                    "system": system,
                    "scope": scope,
                    "external_event_id": external_id,
                },
                "occurred_at": occurred_at,
                "subject": {
                    "kind": "meeting"
                    if system in {"google_calendar", "granola"}
                    else "message",
                    "external_id": external_id,
                },
                "facts": facts,
                "evidence": (),
            }
        )
        events.append(EventToPersist(idempotency_key=key(item), envelope=envelope))
    watermark = max((event.envelope.occurred_at for event in events), default=None)
    return ConnectorPage(tuple(events), cursor, watermark)


class _EnabledSource:
    def __init__(
        self,
        *,
        transport: PageTransport,
        enabled: bool = False,
        allowed_scopes: set[str] | frozenset[str] = frozenset(),
    ):
        self._transport = transport
        self._enabled = enabled is True
        self._allowed_scopes = frozenset(allowed_scopes)

    def _authorize(self, scope: str) -> None:
        if not self._enabled or scope not in self._allowed_scopes:
            raise ConnectorDisabledError("connector unavailable")


class GoogleSheetsSource(_EnabledSource):
    """Google Sheets is read-only and disabled unless a scoped transport is supplied."""

    def __init__(
        self,
        credentials_file: str | None = None,
        *,
        transport: PageTransport | None = None,
        enabled: bool = False,
        allowed_scopes: set[str] | frozenset[str] = frozenset(),
    ):
        if transport is not None:
            super().__init__(
                transport=transport, enabled=enabled, allowed_scopes=allowed_scopes
            )
            self._client = None
            return
        if credentials_file is None:
            raise ValueError("connector configuration invalid")
        from google.oauth2.service_account import Credentials
        import gspread

        credentials = Credentials.from_service_account_file(
            credentials_file, scopes=READ_ONLY_SCOPES
        )
        self._client = gspread.authorize(credentials)
        self._transport = None
        self._enabled = False
        self._allowed_scopes = frozenset()

    def read_values(
        self, spreadsheet_id: str, sheet_name: str
    ) -> Sequence[Sequence[object]]:
        if self._client is None:
            raise ConnectorDisabledError("connector unavailable")
        spreadsheet = self._client.open_by_key(spreadsheet_id)
        return spreadsheet.worksheet(sheet_name).get_all_values()

    def fetch_page(self, scope: str, cursor: str | None) -> ConnectorPage:
        self._authorize(scope)
        assert self._transport is not None
        try:
            raw = self._transport.fetch(scope, cursor)
            return _page(
                raw,
                system="google_sheets",
                scope=scope,
                event_type="sheet.row.observed",
                key=lambda item: _scoped_event_key("google_sheets", scope, item["id"]),
                allowed_fact_names=frozenset(
                    {
                        "id",
                        "occurred_at",
                        "stage",
                        "company_name",
                        "contact_email",
                        "domain",
                        "sector",
                        "commercial_vertical",
                    }
                ),
            )
        except ConnectorDisabledError:
            raise
        except Exception:
            raise RuntimeError("connector fetch failed") from None
