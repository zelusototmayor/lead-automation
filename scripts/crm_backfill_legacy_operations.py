#!/usr/bin/env python3
"""Dry-run-by-default backfill of legacy Lead tasks and note activities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crm.migration.operational_backfill import (  # noqa: E402
    backfill_legacy_operations,
)
from src.crm.migration.sheets_snapshot import load_snapshot  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Immutable snapshot JSON")
    parser.add_argument(
        "--apply", action="store_true", help="Apply one PostgreSQL transaction"
    )
    parser.add_argument("--database-url")
    parser.add_argument("--workspace-id")
    parser.add_argument("--owner-user-id")
    parser.add_argument("--timezone", default="Europe/Lisbon")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and not all(
        (args.database_url, args.workspace_id, args.owner_user_id)
    ):
        parser.error(
            "--apply requires explicit --database-url, --workspace-id and --owner-user-id"
        )
    try:
        report = backfill_legacy_operations(
            load_snapshot(args.snapshot),
            apply=args.apply,
            database_url=args.database_url,
            workspace_id=UUID(args.workspace_id) if args.workspace_id else None,
            owner_user_id=UUID(args.owner_user_id) if args.owner_user_id else None,
            timezone_name=args.timezone,
        )
    except Exception:
        print(
            "error: operational backfill failed; check explicit arguments",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report.safe_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
