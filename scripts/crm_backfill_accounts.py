#!/usr/bin/env python3
"""Dry-run-by-default account backfill from an immutable Sheet snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crm.migration.backfill import backfill_accounts  # noqa: E402
from src.crm.migration.sheets_snapshot import load_snapshot, snapshot_sheet  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", help="Path to a local snapshot JSON file")
    source.add_argument("--fixture", help="Path to local Sheet row fixtures")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="Report only (the default)"
    )
    mode.add_argument(
        "--apply", action="store_true", help="Apply changes to PostgreSQL"
    )
    parser.add_argument(
        "--database-url", help="Explicit postgresql+psycopg URL (required with --apply)"
    )
    parser.add_argument(
        "--workspace-id", help="Explicit workspace UUID (required with --apply)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.database_url is None:
        print(
            "error: apply requires an explicit PostgreSQL database_url", file=sys.stderr
        )
        return 2
    workspace_id = None
    if args.workspace_id is not None:
        from uuid import UUID

        try:
            workspace_id = UUID(args.workspace_id)
        except ValueError:
            build_parser().error("--workspace-id must be a UUID")
    try:
        if args.snapshot is not None:
            snapshot = load_snapshot(args.snapshot)
        else:
            fixture_values = json.loads(Path(args.fixture).read_text())

            class FixtureSource:
                def read_values(self, spreadsheet_id: str, sheet_name: str):
                    return fixture_values

            snapshot = snapshot_sheet(
                FixtureSource(),
                "fixture-spreadsheet",
                "PT Logistics",
                stable_id_column="ID",
            )
        report = backfill_accounts(
            snapshot,
            apply=bool(args.apply),
            database_url=args.database_url,
            workspace_id=workspace_id,
        )
    except Exception:
        print("error: backfill failed; check explicit arguments", file=sys.stderr)
        return 2
    print(json.dumps(report.safe_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
