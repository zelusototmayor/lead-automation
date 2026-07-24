from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.crm.ingestion.contracts import EventEnvelope
from src.crm.persistence.models import (
    Account,
    Contact,
    IngestEvent,
    Lead,
    Proposal,
    Workspace,
)
from tests.migration._postgres import require_disposable_postgres


REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG = REPO_ROOT / "migrations" / "alembic.ini"
NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


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


def _received_gmail_event(
    engine, workspace_id: UUID | None = None
) -> tuple[UUID, UUID]:
    create_workspace = workspace_id is None
    workspace_id = workspace_id or uuid4()
    event_id = uuid4()
    envelope = EventEnvelope.model_validate(
        {
            "schema_version": 1,
            "event_type": "gmail.message.observed",
            "source": {"system": "gmail", "scope": "mailbox:retry"},
            "occurred_at": NOW,
            "subject": {"kind": "message", "external_id": f"message-{event_id}"},
            "facts": {
                "classification": "sent_attachment",
                "thread_id": f"thread-{event_id}",
                "direction": "outbound",
                "contact_email": "retry@example.test",
                "domain": "example.test",
                "company_name": "Retry Buyer",
                "attachment_name": "proposal.pdf",
                "attachment_content_hash": "a" * 64,
            },
        }
    )
    with Session(engine) as session, session.begin():
        if create_workspace:
            session.add(
                Workspace(id=workspace_id, slug=f"w-{workspace_id}", name="Retry Test")
            )
            session.flush()
        session.add(
            IngestEvent(
                id=event_id,
                workspace_id=workspace_id,
                source_system="gmail",
                source_scope="mailbox:retry",
                event_type=envelope.event_type,
                schema_version=1,
                idempotency_key=f"retry-{event_id}",
                occurred_at=NOW,
                payload=envelope.persistence_payload(),
                payload_hash=envelope.payload_hash(),
            )
        )
    return workspace_id, event_id


def _count(session: Session, model, workspace_id) -> int:
    return session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.workspace_id == workspace_id)
    )


def test_unexpected_failure_commits_redacted_retry_state_without_domain_writes(
    engine, monkeypatch
):
    from src.crm.ingestion import processor

    workspace_id, event_id = _received_gmail_event(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def poison(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("secret customer payload")

    monkeypatch.setattr(processor, "_materialize_proposal", poison)
    started_at = datetime.now(UTC)

    outcome = processor.process_ingest_event(factory, workspace_id, event_id)

    assert outcome.status == "failed"
    with Session(engine) as session:
        event = session.get(IngestEvent, event_id)
        assert event is not None
        assert event.processing_status == "failed"
        assert event.attempt_count == 1
        assert event.last_error_redacted == "unexpected processing error"
        assert "secret" not in event.last_error_redacted
        assert event.next_attempt_at is not None
        assert event.next_attempt_at > started_at
        assert _count(session, Account, workspace_id) == 0
        assert _count(session, Contact, workspace_id) == 0
        assert _count(session, Lead, workspace_id) == 0
        assert _count(session, Proposal, workspace_id) == 0


def test_retry_delay_doubles_after_each_failed_attempt(engine, monkeypatch):
    from src.crm.ingestion import processor

    workspace_id, event_id = _received_gmail_event(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def poison(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("private payload")

    monkeypatch.setattr(processor, "_materialize_proposal", poison)
    processor.process_ingest_event(factory, workspace_id, event_id)
    with Session(engine) as session, session.begin():
        event = session.get(IngestEvent, event_id)
        assert event is not None
        event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

    second_started_at = datetime.now(UTC)
    outcome = processor.process_ingest_event(factory, workspace_id, event_id)

    assert outcome.status == "failed"
    with Session(engine) as session:
        event = session.get(IngestEvent, event_id)
        assert event is not None
        assert event.attempt_count == 2
        assert event.next_attempt_at is not None
        delay = event.next_attempt_at - second_started_at
        assert 119 <= delay.total_seconds() <= 121


def test_final_bounded_attempt_moves_event_to_dead_letter(engine, monkeypatch):
    from src.crm.ingestion import processor

    workspace_id, event_id = _received_gmail_event(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        event = session.get(IngestEvent, event_id)
        assert event is not None
        event.processing_status = "failed"
        event.attempt_count = processor.MAX_PROCESSING_ATTEMPTS - 1
        event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

    def poison(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("private payload")

    monkeypatch.setattr(processor, "_materialize_proposal", poison)

    outcome = processor.process_ingest_event(factory, workspace_id, event_id)

    assert outcome.status == "dead_letter"
    with Session(engine) as session:
        event = session.get(IngestEvent, event_id)
        assert event is not None
        assert event.processing_status == "dead_letter"
        assert event.attempt_count == processor.MAX_PROCESSING_ATTEMPTS
        assert event.last_error_redacted == "unexpected processing error"
        assert event.next_attempt_at is None


def test_dead_letter_replay_is_terminal_and_does_not_retry(engine, monkeypatch):
    from src.crm.ingestion import processor

    workspace_id, event_id = _received_gmail_event(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        event = session.get(IngestEvent, event_id)
        assert event is not None
        event.processing_status = "dead_letter"
        event.attempt_count = processor.MAX_PROCESSING_ATTEMPTS
        event.last_error_redacted = "unexpected processing error"
        event.next_attempt_at = None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dead-letter events must not be materialized again")

    monkeypatch.setattr(processor, "_materialize_proposal", forbidden)

    outcome = processor.process_ingest_event(factory, workspace_id, event_id)

    assert outcome.status == "dead_letter"
    with Session(engine) as session:
        event = session.get(IngestEvent, event_id)
        assert event is not None
        assert event.attempt_count == processor.MAX_PROCESSING_ATTEMPTS
        assert event.next_attempt_at is None


def test_malformed_persisted_payload_commits_bounded_retry_state(engine):
    from src.crm.ingestion import processor

    workspace_id, event_id = _received_gmail_event(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        event = session.get(IngestEvent, event_id)
        assert event is not None
        event.payload = {"schema_version": 1, "facts": {"raw": "private"}}

    outcome = processor.process_ingest_event(factory, workspace_id, event_id)

    assert outcome.status == "failed"
    with Session(engine) as session:
        event = session.get(IngestEvent, event_id)
        assert event is not None
        assert event.processing_status == "failed"
        assert event.attempt_count == 1
        assert event.last_error_redacted == "unexpected processing error"
        assert event.next_attempt_at is not None


def test_direct_concurrent_retry_respects_persisted_backoff(engine, monkeypatch):
    from src.crm.ingestion import processor

    workspace_id, event_id = _received_gmail_event(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    future = datetime.now(UTC) + timedelta(hours=1)
    with Session(engine) as session, session.begin():
        event = session.get(IngestEvent, event_id)
        assert event is not None
        event.processing_status = "failed"
        event.attempt_count = 1
        event.next_attempt_at = future

    def forbidden(*_args, **_kwargs):
        raise AssertionError("backed-off event must not be materialized")

    monkeypatch.setattr(processor, "_materialize_proposal", forbidden)

    outcome = processor.process_ingest_event(factory, workspace_id, event_id)

    assert outcome.status == "failed"
    with Session(engine) as session:
        event = session.get(IngestEvent, event_id)
        assert event is not None
        assert event.attempt_count == 1
        assert event.next_attempt_at == future


def test_worker_skips_backoff_event_and_processes_later_eligible_event(
    engine, monkeypatch
):
    from scripts import crm_worker

    workspace_id, backed_off_id = _received_gmail_event(engine)
    _, eligible_id = _received_gmail_event(engine, workspace_id)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        backed_off = session.get(IngestEvent, backed_off_id)
        eligible = session.get(IngestEvent, eligible_id)
        assert backed_off is not None and eligible is not None
        backed_off.processing_status = "failed"
        backed_off.next_attempt_at = now + timedelta(hours=1)
        backed_off.received_at = NOW
        eligible.received_at = NOW + timedelta(minutes=1)

    processed: list[UUID] = []

    def record(factory_arg, workspace_arg, event_id):
        del factory_arg
        assert workspace_arg == workspace_id
        processed.append(event_id)

    monkeypatch.setattr(crm_worker, "process_ingest_event", record)

    eligible_count, processed_count = crm_worker.process_batch(
        factory, workspace_id, limit=10, now=now
    )

    assert eligible_count == 1
    assert processed_count == 1
    assert processed == [eligible_id]


def test_worker_retries_legacy_failed_event_without_backoff_timestamp(
    engine, monkeypatch
):
    from scripts import crm_worker

    workspace_id, event_id = _received_gmail_event(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        event = session.get(IngestEvent, event_id)
        assert event is not None
        event.processing_status = "failed"
        event.next_attempt_at = None

    processed: list[UUID] = []

    def record(factory_arg, workspace_arg, selected_event_id):
        del factory_arg
        assert workspace_arg == workspace_id
        processed.append(selected_event_id)

    monkeypatch.setattr(crm_worker, "process_ingest_event", record)

    eligible_count, processed_count = crm_worker.process_batch(
        factory, workspace_id, limit=10, now=datetime.now(UTC)
    )

    assert eligible_count == 1
    assert processed_count == 1
    assert processed == [event_id]


def test_worker_isolates_poison_event_and_continues_batch(engine, monkeypatch):
    from scripts import crm_worker

    workspace_id, poison_id = _received_gmail_event(engine)
    _, later_id = _received_gmail_event(engine, workspace_id)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        poison = session.get(IngestEvent, poison_id)
        later = session.get(IngestEvent, later_id)
        assert poison is not None and later is not None
        poison.received_at = NOW
        later.received_at = NOW + timedelta(minutes=1)

    attempted: list[UUID] = []

    def process(factory_arg, workspace_arg, event_id):
        del factory_arg
        assert workspace_arg == workspace_id
        attempted.append(event_id)
        if event_id == poison_id:
            raise RuntimeError("poison event")

    monkeypatch.setattr(crm_worker, "process_ingest_event", process)

    eligible_count, processed_count = crm_worker.process_batch(
        factory, workspace_id, limit=10, now=datetime.now(UTC)
    )

    assert eligible_count == 2
    assert processed_count == 1
    assert attempted == [poison_id, later_id]
