from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.crm.connectors.sheets_source import ConnectorPage
from src.crm.ingestion.checkpoints import EventToPersist
from src.crm.ingestion.contracts import EventEnvelope
from src.crm.persistence.models import (
    IngestEvent,
    ReconciliationRun,
    SyncCheckpoint,
    Workspace,
)
from tests.migration._postgres import require_disposable_postgres


REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG = REPO_ROOT / "migrations" / "alembic.ini"


@pytest.fixture(scope="module")
def engine():
    database_url = require_disposable_postgres()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_CONFIG), "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    value = create_engine(database_url)
    yield value
    value.dispose()


class FakeSource:
    def __init__(self, pages):
        self.pages = list(pages)
        self.cursors = []

    def fetch_page(self, scope, cursor):
        self.cursors.append((scope, cursor))
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page


class BlockingSource(FakeSource):
    def __init__(self, pages):
        super().__init__(pages)
        self.first_fetch_started = Event()
        self.second_fetch_started = Event()
        self.release_first_fetch = Event()
        self._lock = Lock()

    def fetch_page(self, scope, cursor):
        with self._lock:
            fetch_number = len(self.cursors)
            self.cursors.append((scope, cursor))
            page = self.pages.pop(0)
        if fetch_number == 0:
            self.first_fetch_started.set()
            if not self.release_first_fetch.wait(timeout=5):
                raise RuntimeError("test synchronization failed")
        else:
            self.second_fetch_started.set()
        return page


def _event(key: str, occurred_at: datetime) -> EventToPersist:
    envelope = EventEnvelope.model_validate(
        {
            "schema_version": 1,
            "event_type": "gmail.message.observed",
            "source": {
                "system": "gmail",
                "scope": "mailbox:commercial",
                "external_event_id": key,
            },
            "occurred_at": occurred_at,
            "subject": {"kind": "message", "external_id": key},
            "facts": {"direction": "outbound"},
            "evidence": [],
        }
    )
    return EventToPersist(idempotency_key=f"gmail:{key}", envelope=envelope)


def _page(cursor: str, *events: EventToPersist) -> ConnectorPage:
    watermark = max((item.envelope.occurred_at for item in events), default=None)
    return ConnectorPage(tuple(events), cursor, watermark)


def _workspace(engine):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=workspace_id, slug=f"w-{workspace_id}", name="Test"))
    return workspace_id


def test_runner_rejects_invalid_configuration_before_fetching(engine):
    from src.crm.ingestion.reconciler import ConnectorRunConfig, run_connector_page

    source = FakeSource([_page("cursor")])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = ConnectorRunConfig(
        workspace_id=_workspace(engine),
        connector="gmail",
        source_scope="mailbox:commercial\nprivate",
        stream="messages",
    )

    with pytest.raises(ValueError, match="invalid connector run configuration"):
        run_connector_page(factory, source, config)

    assert source.cursors == []


def test_runner_replays_from_checkpoint_and_deduplicates_out_of_order_events(engine):
    from src.crm.ingestion.reconciler import ConnectorRunConfig, run_connector_page

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    later = datetime(2026, 7, 16, 12, tzinfo=UTC)
    earlier = datetime(2026, 7, 16, 10, tzinfo=UTC)
    source = FakeSource(
        [
            _page("cursor-1", _event("later", later)),
            _page("cursor-2", _event("later", later), _event("earlier", earlier)),
        ]
    )
    config = ConnectorRunConfig(
        workspace_id=workspace_id,
        connector="gmail",
        source_scope="mailbox:commercial",
        stream="messages",
    )

    first = run_connector_page(factory, source, config)
    second = run_connector_page(factory, source, config)

    assert (first.inserted_count, first.duplicate_count) == (1, 0)
    assert (second.inserted_count, second.duplicate_count) == (1, 1)
    assert source.cursors == [
        ("mailbox:commercial", None),
        ("mailbox:commercial", "cursor-1"),
    ]
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IngestEvent)
                .where(IngestEvent.workspace_id == workspace_id)
            )
            == 2
        )
        checkpoint = session.scalar(
            select(SyncCheckpoint).where(SyncCheckpoint.workspace_id == workspace_id)
        )
        assert checkpoint.cursor_encrypted == "cursor-2"
        assert checkpoint.high_watermark_at == later


def test_runner_records_minimized_reconciliation_run_in_same_transaction(engine):
    from src.crm.ingestion.reconciler import ConnectorRunConfig, run_connector_page

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    occurred_at = datetime(2026, 7, 16, 12, tzinfo=UTC)
    source = FakeSource([_page("cursor-1", _event("new-run", occurred_at))])
    config = ConnectorRunConfig(
        workspace_id=workspace_id,
        connector="gmail",
        source_scope="mailbox:commercial",
        stream="messages",
    )

    result = run_connector_page(factory, source, config)

    assert result.inserted_count == 1
    with Session(engine) as session:
        run = session.scalar(
            select(ReconciliationRun).where(
                ReconciliationRun.workspace_id == workspace_id
            )
        )
        assert run is not None
        assert run.connector == "gmail"
        assert run.source_scope == "mailbox:commercial"
        assert run.status == "succeeded"
        assert run.scanned_count == 1
        assert run.created_count == 1
        assert run.duplicate_count == 0
        assert run.updated_count == 0
        assert run.conflict_count == 0
        assert run.error_count == 0
        assert run.report == {}
        assert run.finished_at is not None
        assert run.finished_at >= run.started_at


def test_runner_serializes_same_checkpoint_before_fetching_next_page(engine):
    from src.crm.ingestion.reconciler import ConnectorRunConfig, run_connector_page

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    source = BlockingSource(
        [
            _page("cursor-1", _event("first", datetime(2026, 7, 16, 10, tzinfo=UTC))),
            _page("cursor-2", _event("second", datetime(2026, 7, 16, 11, tzinfo=UTC))),
        ]
    )
    config = ConnectorRunConfig(
        workspace_id=workspace_id,
        connector="gmail",
        source_scope="mailbox:commercial",
        stream="messages",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_connector_page, factory, source, config)
        assert source.first_fetch_started.wait(timeout=5)
        second = pool.submit(run_connector_page, factory, source, config)
        assert not source.second_fetch_started.wait(timeout=0.25)
        source.release_first_fetch.set()
        assert first.result(timeout=5).inserted_count == 1
        assert second.result(timeout=5).inserted_count == 1

    assert source.cursors == [
        ("mailbox:commercial", None),
        ("mailbox:commercial", "cursor-1"),
    ]
    with Session(engine) as session:
        checkpoint = session.scalar(
            select(SyncCheckpoint).where(SyncCheckpoint.workspace_id == workspace_id)
        )
        assert checkpoint.cursor_encrypted == "cursor-2"


def test_runner_rolls_back_events_and_checkpoint_when_crashing_before_commit(engine):
    from src.crm.ingestion.reconciler import ConnectorRunConfig, run_connector_page

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    source = FakeSource(
        [_page("cursor-secret", _event("new", datetime(2026, 7, 16, tzinfo=UTC)))]
    )
    config = ConnectorRunConfig(
        workspace_id=workspace_id,
        connector="gmail",
        source_scope="mailbox:commercial",
        stream="messages",
    )

    def crash():
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_connector_page(factory, source, config, before_commit=crash)

    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IngestEvent)
                .where(IngestEvent.workspace_id == workspace_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(SyncCheckpoint).where(
                    SyncCheckpoint.workspace_id == workspace_id
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(ReconciliationRun).where(
                    ReconciliationRun.workspace_id == workspace_id
                )
            )
            is None
        )
