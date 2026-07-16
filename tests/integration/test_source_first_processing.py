from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.crm.connectors.calendar_source import CalendarSource
from src.crm.connectors.gmail_source import GmailSource
from src.crm.connectors.meeting_notes_source import MeetingNotesSource
from src.crm.ingestion.reconciler import ConnectorRunConfig, run_connector_page
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    Evidence,
    IngestEvent,
    Lead,
    Proposal,
    ProposalVersion,
    Workspace,
)
from tests.migration._postgres import require_disposable_postgres


REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG = REPO_ROOT / "migrations" / "alembic.ini"
NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


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


class OnePageTransport:
    def __init__(self, item):
        self.item = item

    def fetch(self, scope, cursor):
        del scope, cursor
        return {"items": [self.item], "next_cursor": f"next-{self.item['id']}"}


def _workspace(engine):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=workspace_id, slug=f"w-{workspace_id}", name="Test"))
    return workspace_id


def _ingest(factory, workspace_id, source, connector, scope, stream):
    result = run_connector_page(
        factory,
        source,
        ConnectorRunConfig(
            workspace_id=workspace_id,
            connector=connector,
            source_scope=scope,
            stream=stream,
        ),
    )
    return result.events[0].event_id


def _count(session, model, workspace_id):
    return session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.workspace_id == workspace_id)
    )


def test_gmail_proposal_without_sheet_row_materializes_once(engine):
    from src.crm.ingestion.processor import process_ingest_event

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    scope = "mailbox:commercial"
    source = GmailSource(
        transport=OnePageTransport(
            {
                "id": "message-1",
                "thread_id": "thread-1",
                "occurred_at": NOW,
                "direction": "outbound",
                "classification": "sent_attachment",
                "has_attachments": True,
                "attachment_name": "proposal.pdf",
                "attachment_content_hash": "a" * 64,
                "currency": "EUR",
                "one_off_amount": "12000.00",
                "contact_email": "buyer@example.test",
                "domain": "example.test",
                "company_name": "Example Buyer",
            }
        ),
        enabled=True,
        allowed_scopes={scope},
    )
    event_id = _ingest(factory, workspace_id, source, "gmail", scope, "messages")

    first = process_ingest_event(factory, workspace_id, event_id)
    second = process_ingest_event(factory, workspace_id, event_id)

    assert first.status == second.status == "applied"
    with Session(engine) as session:
        assert _count(session, Account, workspace_id) == 1
        assert _count(session, Contact, workspace_id) == 1
        assert _count(session, Lead, workspace_id) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(Activity)
                .where(
                    Activity.workspace_id == workspace_id,
                    Activity.activity_type == "email_sent",
                )
            )
            == 1
        )
        assert _count(session, Proposal, workspace_id) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProposalVersion)
                .join(Proposal, Proposal.id == ProposalVersion.proposal_id)
                .where(Proposal.workspace_id == workspace_id)
            )
            == 1
        )
        event = session.get(IngestEvent, event_id)
        assert event.processing_status == "applied"
        assert event.applied_at is not None


def test_processing_crash_rolls_back_domain_and_leaves_event_retryable(engine):
    from src.crm.ingestion.processor import process_ingest_event

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    scope = "mailbox:commercial"
    source = GmailSource(
        transport=OnePageTransport(
            {
                "id": "message-crash",
                "thread_id": "thread-crash",
                "occurred_at": NOW,
                "direction": "outbound",
                "classification": "sent_attachment",
                "has_attachments": True,
                "attachment_name": "proposal.pdf",
                "attachment_content_hash": "b" * 64,
                "currency": "EUR",
                "one_off_amount": "9000.00",
                "contact_email": "crash@example.test",
                "domain": "example.test",
                "company_name": "Crash Buyer",
            }
        ),
        enabled=True,
        allowed_scopes={scope},
    )
    event_id = _ingest(factory, workspace_id, source, "gmail", scope, "messages")

    def crash():
        raise RuntimeError("simulated processing crash")

    with pytest.raises(RuntimeError, match="simulated processing crash"):
        process_ingest_event(factory, workspace_id, event_id, before_commit=crash)

    with Session(engine) as session:
        assert _count(session, Account, workspace_id) == 0
        assert _count(session, Contact, workspace_id) == 0
        assert _count(session, Lead, workspace_id) == 0
        assert _count(session, Proposal, workspace_id) == 0
        event = session.get(IngestEvent, event_id)
        assert event.processing_status == "received"
        assert event.attempt_count == 0

    assert process_ingest_event(factory, workspace_id, event_id).status == "applied"


def test_calendar_commercial_meeting_materializes_but_personal_event_is_ignored(engine):
    from src.crm.ingestion.processor import process_ingest_event

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    scope = "calendar:commercial"
    commercial = CalendarSource(
        transport=OnePageTransport(
            {
                "id": "calendar-1",
                "updated": "2026-07-16T12:30:00+00:00",
                "occurred_at": NOW,
                "classification": "confirmed",
                "status": "booked",
                "contact_email": "buyer@calendar.test",
                "domain": "calendar.test",
                "company_name": "Calendar Buyer",
            }
        ),
        enabled=True,
        allowed_scopes={scope},
    )
    commercial_id = _ingest(
        factory, workspace_id, commercial, "google_calendar", scope, "events"
    )
    assert (
        process_ingest_event(factory, workspace_id, commercial_id).status == "applied"
    )

    personal = CalendarSource(
        transport=OnePageTransport(
            {
                "id": "calendar-personal",
                "updated": "2026-07-16T12:31:00+00:00",
                "occurred_at": NOW,
                "classification": "excluded",
                "status": "booked",
            }
        ),
        enabled=True,
        allowed_scopes={scope},
    )
    personal_id = _ingest(
        factory, workspace_id, personal, "google_calendar", scope, "events"
    )
    assert process_ingest_event(factory, workspace_id, personal_id).status == "ignored"

    with Session(engine) as session:
        assert _count(session, Account, workspace_id) == 1
        assert _count(session, Contact, workspace_id) == 1
        assert _count(session, Lead, workspace_id) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(Activity)
                .where(
                    Activity.workspace_id == workspace_id,
                    Activity.activity_type == "meeting",
                )
            )
            == 1
        )
        assert session.get(IngestEvent, personal_id).processing_status == "ignored"


def test_ambiguous_exact_account_match_marks_event_for_review(engine):
    from src.crm.ingestion.processor import process_ingest_event

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        by_email = Account(
            workspace_id=workspace_id,
            display_name="Email Match",
            normalized_name="email match",
        )
        by_domain = Account(
            workspace_id=workspace_id,
            display_name="Domain Match",
            normalized_name="domain match",
            primary_domain="ambiguous.test",
        )
        session.add_all([by_email, by_domain])
        session.flush()
        session.add(
            Contact(
                workspace_id=workspace_id,
                account_id=by_email.id,
                primary_email="buyer@ambiguous.test",
            )
        )

    scope = "calendar:commercial"
    source = CalendarSource(
        transport=OnePageTransport(
            {
                "id": "calendar-ambiguous",
                "updated": "2026-07-16T12:32:00+00:00",
                "occurred_at": NOW,
                "classification": "confirmed",
                "status": "booked",
                "contact_email": "buyer@ambiguous.test",
                "domain": "ambiguous.test",
                "company_name": "Domain Match",
            }
        ),
        enabled=True,
        allowed_scopes={scope},
    )
    event_id = _ingest(
        factory, workspace_id, source, "google_calendar", scope, "events"
    )

    outcome = process_ingest_event(factory, workspace_id, event_id)

    assert outcome.status == "review"
    with Session(engine) as session:
        assert _count(session, Account, workspace_id) == 2
        assert _count(session, Lead, workspace_id) == 0
        event = session.get(IngestEvent, event_id)
        assert event.processing_status == "review"
        assert event.last_error_redacted == "identity requires review"


def test_meeting_notes_without_sheet_row_create_minimized_evidence(engine):
    from src.crm.ingestion.processor import process_ingest_event

    workspace_id = _workspace(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    scope = "granola:team"
    source = MeetingNotesSource(
        transport=OnePageTransport(
            {
                "id": "note-1",
                "meeting_external_id": "meeting-1",
                "occurred_at": NOW,
                "classification": "confirmed",
                "has_notes": True,
                "contact_email": "buyer@notes.test",
                "domain": "notes.test",
                "company_name": "Notes Buyer",
            }
        ),
        enabled=True,
        allowed_scopes={scope},
    )
    event_id = _ingest(factory, workspace_id, source, "granola", scope, "notes")

    assert process_ingest_event(factory, workspace_id, event_id).status == "applied"

    with Session(engine) as session:
        assert _count(session, Account, workspace_id) == 1
        assert _count(session, Contact, workspace_id) == 1
        assert _count(session, Lead, workspace_id) == 1
        evidence = session.scalar(
            select(Evidence).where(Evidence.workspace_id == workspace_id)
        )
        assert evidence.evidence_type == "meeting_note"
        assert evidence.excerpt_redacted is None
        assert evidence.metadata_json == {"has_notes": True}
