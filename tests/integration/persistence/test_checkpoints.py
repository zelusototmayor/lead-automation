from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_CONFIG = REPO_ROOT / "migrations" / "alembic.ini"


@pytest.fixture(scope="module")
def engine():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql+psycopg://"):
        pytest.skip("requires disposable PostgreSQL")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_CONFIG), "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    db_engine = create_engine(database_url)
    yield db_engine
    db_engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE sync_checkpoints, ingest_events, source_identities, workspaces CASCADE"
            )
        )


def envelope(*, status: str = "new"):
    from src.crm.ingestion.contracts import EventEnvelope

    return EventEnvelope.model_validate(
        {
            "schema_version": 1,
            "event_type": "message.received",
            "source": {
                "system": "gmail",
                "scope": "inbox-a",
                "external_event_id": "evt-1",
            },
            "occurred_at": "2026-07-15T10:00:00Z",
            "subject": {"kind": "message", "external_id": "msg-1"},
            "facts": {"status": status},
            "evidence": [],
        }
    )


def workspace(session: Session, slug: str = "workspace-a") -> UUID:
    from src.crm.persistence.models import Workspace

    row = Workspace(slug=slug, name=slug)
    session.add(row)
    session.flush()
    return row.id


def key(workspace_id: UUID, *, scope: str = "inbox-a"):
    from src.crm.ingestion.checkpoints import CheckpointKey

    return CheckpointKey(
        workspace_id=workspace_id,
        connector="gmail",
        source_scope=scope,
        stream="messages",
    )


def event(idempotency_key: str, *, status: str = "new"):
    from src.crm.ingestion.checkpoints import EventToPersist

    return EventToPersist(
        idempotency_key=idempotency_key, envelope=envelope(status=status)
    )


def test_successful_batch_stores_events_and_checkpoint_atomically(engine) -> None:
    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import IngestEvent, SyncCheckpoint

    watermark = datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        result = persist_event_batch_and_advance_checkpoint(
            session,
            key(workspace_id),
            "opaque-ciphertext-page-2",
            [event("one"), event("two")],
            high_watermark_at=watermark,
        )
        assert result.inserted_count == 2
        assert result.duplicate_count == 0
        assert len(result.events) == 2
        checkpoint = session.scalar(select(SyncCheckpoint))
        assert checkpoint is not None
        assert checkpoint.cursor_encrypted == "opaque-ciphertext-page-2"
        assert checkpoint.high_watermark_at == watermark
        assert checkpoint.last_success_at is not None
        assert checkpoint.last_error_redacted is None
        assert checkpoint.consecutive_failures == 0
        assert len(session.scalars(select(IngestEvent)).all()) == 2


def test_failed_batch_rolls_back_new_event_and_checkpoint_but_session_is_usable(
    engine,
) -> None:
    from src.crm.ingestion.checkpoints import (
        IdempotencyConflictError,
        persist_event_batch_and_advance_checkpoint,
    )
    from src.crm.persistence.models import IngestEvent, SyncCheckpoint

    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "ciphertext-page-1", [event("existing")]
        )

    with Session(engine) as session, session.begin():
        with pytest.raises(IdempotencyConflictError):
            persist_event_batch_and_advance_checkpoint(
                session,
                key(workspace_id),
                "ciphertext-page-2",
                [event("new-before-conflict"), event("existing", status="changed")],
            )
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        assert {
            row.idempotency_key for row in session.scalars(select(IngestEvent))
        } == {"existing"}
        checkpoint = session.scalar(select(SyncCheckpoint))
        assert checkpoint is not None
        assert checkpoint.cursor_encrypted == "ciphertext-page-1"


def test_initial_failed_batch_creates_nothing(engine) -> None:
    from src.crm.ingestion.checkpoints import (
        IdempotencyConflictError,
        persist_event_batch_and_advance_checkpoint,
        record_ingest_event,
    )
    from src.crm.persistence.models import IngestEvent, SyncCheckpoint

    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        record_ingest_event(session, workspace_id, "conflict", envelope())

    with Session(engine) as session, session.begin():
        with pytest.raises(IdempotencyConflictError):
            persist_event_batch_and_advance_checkpoint(
                session,
                key(workspace_id, scope="never-created"),
                "ciphertext",
                [event("temporary"), event("conflict", status="changed")],
            )
        assert session.scalar(select(SyncCheckpoint)) is None
        assert {
            row.idempotency_key for row in session.scalars(select(IngestEvent))
        } == {"conflict"}


def test_replay_batch_dedupes_and_advances_checkpoint(engine) -> None:
    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import IngestEvent, SyncCheckpoint

    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "ciphertext-1", [event("one")]
        )
        replay = persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "ciphertext-2", [event("one")]
        )
        assert replay.inserted_count == 0
        assert replay.duplicate_count == 1
        assert len(session.scalars(select(IngestEvent)).all()) == 1
        assert session.scalar(select(SyncCheckpoint)).cursor_encrypted == "ciphertext-2"


def test_empty_successful_page_explicitly_advances_checkpoint(engine) -> None:
    """An empty fetched page is successful and advances its opaque cursor."""
    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import SyncCheckpoint

    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        result = persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "ciphertext-empty", []
        )
        assert result.events == ()
        assert result.inserted_count == result.duplicate_count == 0
        assert (
            session.scalar(select(SyncCheckpoint)).cursor_encrypted
            == "ciphertext-empty"
        )


def test_checkpoint_workspace_and_scope_are_isolated_and_unique(engine) -> None:
    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import SyncCheckpoint

    with Session(engine) as session, session.begin():
        first_workspace = workspace(session, "first")
        second_workspace = workspace(session, "second")
        persist_event_batch_and_advance_checkpoint(
            session, key(first_workspace, scope="a"), "ciphertext-a", []
        )
        persist_event_batch_and_advance_checkpoint(
            session, key(first_workspace, scope="b"), "ciphertext-b", []
        )
        persist_event_batch_and_advance_checkpoint(
            session, key(second_workspace, scope="a"), "ciphertext-c", []
        )
        assert len(session.scalars(select(SyncCheckpoint)).all()) == 3
        session.add(
            SyncCheckpoint(
                workspace_id=first_workspace,
                connector="gmail",
                source_scope="a",
                stream="messages",
                cursor_encrypted="duplicate",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_repository_never_commits_outer_transaction(engine) -> None:
    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import IngestEvent, SyncCheckpoint, Workspace

    session = Session(engine)
    try:
        workspace_id = workspace(session)
        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "ciphertext", [event("one")]
        )
        session.rollback()
    finally:
        session.close()

    with Session(engine) as verification:
        assert verification.scalar(select(Workspace)) is None
        assert verification.scalar(select(IngestEvent)) is None
        assert verification.scalar(select(SyncCheckpoint)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("idempotency_key", "x" * 513),
        ("idempotency_key", "secret\nmarker"),
        ("connector", "x" * 65),
        ("source_scope", "x" * 256),
        ("stream", "x" * 129),
        ("cursor", "x" * 65537),
        ("cursor", "secret\x00marker"),
        ("watermark", datetime(2026, 7, 15)),
    ],
)
def test_repository_rejects_bounded_inputs_generically_and_session_remains_usable(
    engine, field, value
) -> None:
    from src.crm.ingestion.checkpoints import (
        CheckpointKey,
        EventToPersist,
        InvalidIngestionInputError,
        persist_event_batch_and_advance_checkpoint,
    )

    marker = str(value)
    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        checkpoint_key = CheckpointKey(
            workspace_id=workspace_id,
            connector=value if field == "connector" else "gmail",
            source_scope=value if field == "source_scope" else "scope",
            stream=value if field == "stream" else "messages",
        )
        items = [
            EventToPersist(value if field == "idempotency_key" else "event", envelope())
        ]
        with pytest.raises(InvalidIngestionInputError) as exc_info:
            persist_event_batch_and_advance_checkpoint(
                session,
                checkpoint_key,
                value if field == "cursor" else "encrypted-cursor",
                items,
                high_watermark_at=value if field == "watermark" else None,
            )
        assert marker not in str(exc_info.value)
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_high_watermark_is_normalized_to_utc(engine) -> None:
    from datetime import timedelta, timezone

    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import SyncCheckpoint

    shifted = datetime(2026, 7, 15, 12, tzinfo=timezone(timedelta(hours=2)))
    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "encrypted", [], shifted
        )
        assert session.scalar(select(SyncCheckpoint)).high_watermark_at == datetime(
            2026, 7, 15, 10, tzinfo=UTC
        )


def test_high_watermark_is_monotonic_while_opaque_cursor_always_advances(
    engine,
) -> None:
    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import SyncCheckpoint

    t1 = datetime(2026, 7, 15, 9, tzinfo=UTC)
    t2 = datetime(2026, 7, 15, 10, tzinfo=UTC)
    t3 = datetime(2026, 7, 15, 11, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "cursor-t2", [], t2
        )
        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "cursor-none", [], None
        )
        checkpoint = session.scalar(select(SyncCheckpoint))
        assert checkpoint.high_watermark_at == t2
        assert checkpoint.cursor_encrypted == "cursor-none"

        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "cursor-t1", [], t1
        )
        session.refresh(checkpoint)
        assert checkpoint.high_watermark_at == t2
        assert checkpoint.cursor_encrypted == "cursor-t1"

        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "cursor-t3", [], t3
        )
        session.refresh(checkpoint)
        assert checkpoint.high_watermark_at == t3
        assert checkpoint.cursor_encrypted == "cursor-t3"


@pytest.mark.parametrize("initial", [None, datetime(2026, 7, 15, 9, tzinfo=UTC)])
def test_initial_high_watermark_preserves_none_or_stores_supplied_value(
    engine, initial
) -> None:
    from src.crm.ingestion.checkpoints import persist_event_batch_and_advance_checkpoint
    from src.crm.persistence.models import SyncCheckpoint

    with Session(engine) as session, session.begin():
        workspace_id = workspace(session)
        persist_event_batch_and_advance_checkpoint(
            session, key(workspace_id), "initial", [], initial
        )
        assert session.scalar(select(SyncCheckpoint)).high_watermark_at == initial
