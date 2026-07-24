#!/usr/bin/env python3
"""Create a local immutable snapshot through the read-only Sheets connector."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crm.connectors.sheets_source import GoogleSheetsSource  # noqa: E402
from src.crm.migration.sheets_snapshot import save_snapshot, snapshot_sheet  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--sheet-name", required=True)
    parser.add_argument("--stable-id-column", required=True)
    parser.add_argument(
        "--fallback-identity",
        action="append",
        default=[],
        metavar="COLUMN[,COLUMN...]",
        help="Explicit ordered identity fallback group; repeat for lower-priority groups",
    )
    parser.add_argument("--output", required=True, help="Local JSON snapshot path")
    parser.add_argument(
        "--save", action="store_true", help="Write the local snapshot file"
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    source_factory: Callable[[str], object] = GoogleSheetsSource,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = source_factory(args.credentials_file)
        fallback_groups = tuple(
            tuple(column.strip() for column in group.split(","))
            for group in args.fallback_identity
        )
        snapshot = snapshot_sheet(
            source,  # type: ignore[arg-type]
            args.spreadsheet_id,
            args.sheet_name,
            stable_id_column=args.stable_id_column,
            fallback_identity_columns=fallback_groups,
        )
        if args.save:
            save_snapshot(snapshot, args.output)
    except Exception:
        print("error: snapshot failed; check explicit arguments", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "input_rows": snapshot.input_rows,
                "snapshot_rows": len(snapshot.rows),
                "duplicates": len(snapshot.duplicate_ids),
                "conflicts": len(snapshot.missing_id_rows),
                "saved": bool(args.save),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
