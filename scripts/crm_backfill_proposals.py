#!/usr/bin/env python3
"""Dry-run-by-default legacy proposal backfill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crm.migration.proposals_backfill import backfill_proposals  # noqa: E402
from src.crm.migration.sheets_snapshot import load_snapshot  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument("--workspace-id")
    args = parser.parse_args(argv)
    if args.apply and (not args.database_url or not args.workspace_id):
        parser.error("--apply requires explicit --database-url and --workspace-id")
    try:
        report = backfill_proposals(
            load_snapshot(args.snapshot),
            apply=args.apply,
            database_url=args.database_url,
            workspace_id=UUID(args.workspace_id) if args.workspace_id else None,
        )
    except Exception:
        print(
            "error: proposal backfill failed; check explicit arguments", file=sys.stderr
        )
        return 2
    print(json.dumps(report.safe_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
