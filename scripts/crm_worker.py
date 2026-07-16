#!/usr/bin/env python3
"""Fail-closed CRM ingest-event worker; never performs outbound actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from dashboard.app.db import (  # noqa: E402
    create_database_engine,
    create_session_factory,
)
from src.crm.ingestion.processor import process_ingest_event  # noqa: E402
from src.crm.persistence.models import IngestEvent  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process queued CRM events")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.limit <= 1000:
        print("CRM operation unavailable", file=sys.stderr)
        return 2
    if not args.apply:
        print(json.dumps({"apply": False, "eligible_count": 0, "processed_count": 0}))
        return 0

    engine = None
    try:
        engine = create_database_engine()
        factory = create_session_factory(engine)
        with Session(engine) as session:
            eligible_count = session.scalar(
                select(func.count())
                .select_from(IngestEvent)
                .where(
                    IngestEvent.workspace_id == args.workspace_id,
                    IngestEvent.processing_status.in_(("received", "failed")),
                )
            )
            event_ids = tuple(
                session.scalars(
                    select(IngestEvent.id)
                    .where(
                        IngestEvent.workspace_id == args.workspace_id,
                        IngestEvent.processing_status.in_(("received", "failed")),
                    )
                    .order_by(IngestEvent.received_at, IngestEvent.id)
                    .limit(args.limit)
                )
            )
        processed = 0
        for event_id in event_ids:
            process_ingest_event(factory, args.workspace_id, event_id)
            processed += 1
        print(
            json.dumps(
                {
                    "apply": True,
                    "eligible_count": int(eligible_count or 0),
                    "processed_count": processed,
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
