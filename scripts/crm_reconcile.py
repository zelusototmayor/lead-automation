#!/usr/bin/env python3
"""Reconcile one local connector fixture into the CRM ledger, disabled by default."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard.app.db import (  # noqa: E402
    create_database_engine,
    create_session_factory,
)
from src.crm.connectors.calendar_source import CalendarSource  # noqa: E402
from src.crm.connectors.gmail_source import GmailSource  # noqa: E402
from src.crm.connectors.meeting_notes_source import MeetingNotesSource  # noqa: E402
from src.crm.connectors.sheets_source import GoogleSheetsSource  # noqa: E402
from src.crm.ingestion.reconciler import (  # noqa: E402
    ConnectorRunConfig,
    run_connector_page,
)


class _FixtureTransport:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def fetch(self, scope: str, cursor: str | None) -> dict[str, Any]:
        del scope, cursor
        return self._payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile one CRM connector page")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument(
        "--connector", required=True, choices=("gmail", "calendar", "granola", "sheets")
    )
    parser.add_argument("--source-scope", required=True)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser


def _source(connector: str, transport: _FixtureTransport, scope: str):
    common = {"transport": transport, "enabled": True, "allowed_scopes": {scope}}
    if connector == "gmail":
        return GmailSource(**common)
    if connector == "calendar":
        return CalendarSource(**common)
    if connector == "granola":
        return MeetingNotesSource(**common)
    return GoogleSheetsSource(**common)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = None
    try:
        raw = json.loads(args.fixture.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("invalid fixture")
        source = _source(args.connector, _FixtureTransport(raw), args.source_scope)
        page = source.fetch_page(args.source_scope, None)
        if not args.apply:
            print(
                json.dumps(
                    {
                        "apply": False,
                        "duplicate_count": 0,
                        "event_count": len(page.events),
                        "inserted_count": 0,
                    }
                )
            )
            return 0

        engine = create_database_engine()
        factory = create_session_factory(engine)
        result = run_connector_page(
            factory,
            source,
            ConnectorRunConfig(
                workspace_id=args.workspace_id,
                connector=args.connector,
                source_scope=args.source_scope,
                stream=args.stream,
            ),
        )
        print(
            json.dumps(
                {
                    "apply": True,
                    "duplicate_count": result.duplicate_count,
                    "event_count": len(result.events),
                    "inserted_count": result.inserted_count,
                }
            )
        )
        return 0
    except Exception:
        print("CRM operation unavailable", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
