#!/usr/bin/env python3
"""Read-only aggregate comparison of a legacy snapshot with PostgreSQL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crm.migration.compare import compare_legacy  # noqa: E402
from src.crm.migration.sheets_snapshot import load_snapshot  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument(
        "--database-url", required=True, help="Explicit postgresql+psycopg URL"
    )
    parser.add_argument("--workspace-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace_id = UUID(args.workspace_id)
        report = compare_legacy(
            load_snapshot(args.snapshot),
            database_url=args.database_url,
            workspace_id=workspace_id,
        )
    except Exception:
        print("error: comparison failed; check explicit arguments", file=sys.stderr)
        return 2
    print(json.dumps(report.safe_dict(), sort_keys=True))
    return 0 if report.parity else 1


if __name__ == "__main__":
    raise SystemExit(main())
