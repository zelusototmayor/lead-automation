"""Checkpointed one-page connector reconciliation.

Fetching is read-only. Event persistence and checkpoint advancement share the
caller's single database transaction, so a crash cannot advance one without the
other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from typing import Callable, Protocol
import unicodedata
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from src.crm.connectors.sheets_source import ConnectorPage
from src.crm.ingestion.checkpoints import (
    BatchPersistResult,
    CheckpointKey,
    persist_event_batch_and_advance_checkpoint,
)
from src.crm.persistence.models import ReconciliationRun, SyncCheckpoint


class ConnectorSource(Protocol):
    def fetch_page(self, scope: str, cursor: str | None) -> ConnectorPage: ...


@dataclass(frozen=True, slots=True)
class ConnectorRunConfig:
    workspace_id: UUID
    connector: str
    source_scope: str
    stream: str


def _validate_config(config: object) -> ConnectorRunConfig:
    if type(config) is not ConnectorRunConfig or type(config.workspace_id) is not UUID:
        raise ValueError("invalid connector run configuration")
    for value, maximum in (
        (config.connector, 64),
        (config.source_scope, 255),
        (config.stream, 128),
    ):
        if (
            type(value) is not str
            or not value.strip()
            or len(value) > maximum
            or any(
                unicodedata.category(character) in {"Cc", "Cf"} for character in value
            )
        ):
            raise ValueError("invalid connector run configuration")
    return config


def _checkpoint_cursor(session: Session, config: ConnectorRunConfig) -> str | None:
    return session.scalar(
        select(SyncCheckpoint.cursor_encrypted).where(
            SyncCheckpoint.workspace_id == config.workspace_id,
            SyncCheckpoint.connector == config.connector,
            SyncCheckpoint.source_scope == config.source_scope,
            SyncCheckpoint.stream == config.stream,
        )
    )


def _run_lock_key(config: ConnectorRunConfig) -> int:
    canonical = json.dumps(
        [
            str(config.workspace_id),
            config.connector,
            config.source_scope,
            config.stream,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def run_connector_page(
    session_factory: sessionmaker[Session],
    source: ConnectorSource,
    config: ConnectorRunConfig,
    *,
    before_commit: Callable[[], None] | None = None,
) -> BatchPersistResult:
    """Fetch and durably record one connector page.

    ``before_commit`` is a fault-injection seam used to prove crash rollback. It
    runs after SQL has been issued but before the outer transaction commits.
    """

    config = _validate_config(config)

    key = CheckpointKey(
        workspace_id=config.workspace_id,
        connector=config.connector,
        source_scope=config.source_scope,
        stream=config.stream,
    )
    with session_factory() as session, session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _run_lock_key(config)},
        )
        cursor = _checkpoint_cursor(session, config)
        started_at = datetime.now(UTC)
        page = source.fetch_page(config.source_scope, cursor)
        if type(page) is not ConnectorPage:
            raise RuntimeError("connector fetch failed")
        result = persist_event_batch_and_advance_checkpoint(
            session,
            key,
            page.next_cursor,
            page.events,
            high_watermark_at=page.high_watermark_at,
        )
        finished_at = datetime.now(UTC)
        window_end_at = page.high_watermark_at or finished_at
        session.add(
            ReconciliationRun(
                workspace_id=config.workspace_id,
                connector=config.connector,
                source_scope=config.source_scope,
                window_start_at=window_end_at,
                window_end_at=window_end_at,
                started_at=started_at,
                finished_at=finished_at,
                status="succeeded",
                scanned_count=len(page.events),
                created_count=result.inserted_count,
                updated_count=0,
                duplicate_count=result.duplicate_count,
                conflict_count=0,
                error_count=0,
                report={},
            )
        )
        if before_commit is not None:
            before_commit()
        return result
