from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.crm.persistence.models import (
    Account,
    EmailMessage,
    Evidence,
    Meeting,
    Proposal,
    ReconciliationRun,
    SourceIdentity,
    Task,
    Workspace,
)
from scripts.crm_verify_backup import _smoke_restored_database, validate_safe_target
from tests.migration._postgres import require_disposable_postgres


REPO_ROOT = Path(__file__).resolve().parents[3]
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
    try:
        yield value
    finally:
        value.dispose()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_CONFIG),
                "downgrade",
                "base",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_CONFIG),
                "upgrade",
                "head",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )


def test_email_message_identity_is_unique_per_mailbox_and_workspace(engine):
    workspace_id = uuid4()
    mailbox_identity_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"canonical-mail-{workspace_id}",
                name="Canonical Mail Test",
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Canonical Mail Test",
            normalized_name="canonical mail test",
        )
        mailbox = SourceIdentity(
            id=mailbox_identity_id,
            workspace_id=workspace_id,
            source_system="gmail",
            source_scope="mailbox:test",
            entity_kind="mailbox",
            external_id="mailbox:test",
            metadata_json={},
        )
        session.add_all([account, mailbox])
        session.flush()
        session.add(
            EmailMessage(
                workspace_id=workspace_id,
                account_id=account.id,
                mailbox_identity_id=mailbox_identity_id,
                provider_message_id="message-1",
                provider_thread_id="thread-1",
                direction="outbound",
                sent_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
        )

    with Session(engine) as session:
        account_id = session.scalar(
            select(Account.id).where(Account.workspace_id == workspace_id)
        )
        session.add(
            EmailMessage(
                workspace_id=workspace_id,
                account_id=account_id,
                mailbox_identity_id=mailbox_identity_id,
                provider_message_id="message-1",
                provider_thread_id="thread-1",
                direction="outbound",
                sent_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(EmailMessage)
                .where(EmailMessage.workspace_id == workspace_id)
            )
            == 1
        )


def test_backup_smoke_accepts_valid_canonical_mailbox_identity(engine):
    database_url = require_disposable_postgres()
    target = validate_safe_target(database_url, disposable_marker=True)

    result = _smoke_restored_database(target, target.database)

    assert result["status"] == "verified"
    assert result["schema_revision"] == "0009"
    assert result["invariant_violations"] == 0


def test_meeting_occurrence_identity_is_unique_and_status_is_explicit(engine):
    workspace_id = uuid4()
    start = datetime(2026, 7, 18, 10, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"canonical-meeting-{workspace_id}",
                name="Canonical Meeting Test",
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Canonical Meeting Test",
            normalized_name="canonical meeting test",
        )
        session.add(account)
        session.flush()
        session.add(
            Meeting(
                workspace_id=workspace_id,
                account_id=account.id,
                provider="google_calendar",
                calendar_id="commercial",
                external_event_id="event-1",
                occurrence_start_at=start,
                scheduled_start_at=start,
                status="booked",
            )
        )

    with Session(engine) as session:
        account_id = session.scalar(
            select(Account.id).where(Account.workspace_id == workspace_id)
        )
        session.add(
            Meeting(
                workspace_id=workspace_id,
                account_id=account_id,
                provider="google_calendar",
                calendar_id="commercial",
                external_event_id="event-1",
                occurrence_start_at=start,
                scheduled_start_at=start,
                status="held",
                held_at=start,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_task_persists_account_context_without_optional_proposal(engine):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"canonical-task-{workspace_id}",
                name="Canonical Task Test",
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Canonical Task Test",
            normalized_name="canonical task test",
        )
        session.add(account)
        session.flush()
        task = Task(
            workspace_id=workspace_id,
            account_id=account.id,
            task_type="follow_up",
            title="Follow up with buyer",
            due_at=datetime(2026, 7, 19, tzinfo=UTC),
            owner_user_id=uuid4(),
            status="open",
            source_rule="proposal_stale",
        )
        session.add(task)

    with Session(engine) as session:
        persisted = session.scalar(
            select(Task).where(Task.workspace_id == workspace_id)
        )
        assert persisted is not None
        assert persisted.account_id is not None
        assert persisted.proposal_id is None
        assert persisted.version == 1


def test_task_rejects_proposal_from_another_account(engine):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"canonical-task-proposal-{workspace_id}",
                name="Canonical Task Proposal Test",
            )
        )
        session.flush()
        task_account = Account(
            workspace_id=workspace_id,
            display_name="Task Account",
            normalized_name=f"task account {workspace_id}",
        )
        proposal_account = Account(
            workspace_id=workspace_id,
            display_name="Proposal Account",
            normalized_name=f"proposal account {workspace_id}",
        )
        session.add_all([task_account, proposal_account])
        session.flush()
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=proposal_account.id,
            title="Foreign account proposal",
            currency="EUR",
        )
        session.add(proposal)
        session.flush()
        session.add(
            Task(
                workspace_id=workspace_id,
                account_id=task_account.id,
                proposal_id=proposal.id,
                task_type="follow_up",
                title="Invalid cross-account task",
                due_at=datetime(2026, 7, 19, tzinfo=UTC),
                owner_user_id=uuid4(),
                status="open",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_reconciliation_run_persists_minimized_report_and_counts(engine):
    workspace_id = uuid4()
    started_at = datetime(2026, 7, 18, 12, tzinfo=UTC)
    finished_at = datetime(2026, 7, 18, 12, 5, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"canonical-reconciliation-{workspace_id}",
                name="Canonical Reconciliation Test",
            )
        )
        session.flush()
        session.add(
            ReconciliationRun(
                workspace_id=workspace_id,
                connector="gmail",
                source_scope="mailbox:sales@example.test",
                window_start_at=datetime(2026, 7, 17, tzinfo=UTC),
                window_end_at=datetime(2026, 7, 18, tzinfo=UTC),
                started_at=started_at,
                finished_at=finished_at,
                status="succeeded",
                scanned_count=10,
                created_count=2,
                updated_count=3,
                duplicate_count=4,
                conflict_count=1,
                error_count=0,
                report={"conflict": 1},
            )
        )

    with Session(engine) as session:
        run = session.scalar(
            select(ReconciliationRun).where(
                ReconciliationRun.workspace_id == workspace_id
            )
        )
        assert run is not None
        assert run.scanned_count == 10
        assert run.report == {"conflict": 1}


def test_task_rejects_completed_status_without_completion_activity(engine):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"canonical-task-completion-{workspace_id}",
                name="Canonical Task Completion Test",
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Task Completion Account",
            normalized_name=f"task completion account {workspace_id}",
        )
        session.add(account)
        session.flush()
        session.add(
            Task(
                workspace_id=workspace_id,
                account_id=account.id,
                task_type="follow_up",
                title="Incomplete completion evidence",
                due_at=datetime(2026, 7, 19, tzinfo=UTC),
                owner_user_id=uuid4(),
                status="completed",
                completed_at=datetime(2026, 7, 18, 13, tzinfo=UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.parametrize(
    "overrides",
    [
        {"scanned_count": -1},
        {"status": "running", "finished_at": datetime(2026, 7, 18, 12, 5, tzinfo=UTC)},
        {"report": {"detail": "x" * 4097}},
        {"report": {"payload": "private"}},
        {"report": {"conflict": {"raw": "private"}}},
    ],
)
def test_reconciliation_run_rejects_invalid_state_counts_and_report(engine, overrides):
    workspace_id = uuid4()
    values = {
        "workspace_id": workspace_id,
        "connector": "gmail",
        "source_scope": "mailbox:test",
        "window_start_at": datetime(2026, 7, 17, tzinfo=UTC),
        "window_end_at": datetime(2026, 7, 18, tzinfo=UTC),
        "started_at": datetime(2026, 7, 18, 12, tzinfo=UTC),
        "finished_at": datetime(2026, 7, 18, 12, 5, tzinfo=UTC),
        "status": "succeeded",
        "report": {},
    }
    values.update(overrides)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"invalid-reconciliation-{workspace_id}",
                name="Invalid Reconciliation Test",
            )
        )
        session.flush()
        session.add(ReconciliationRun(**values))
        with pytest.raises(IntegrityError):
            session.flush()


def _account_with_identity(session: Session, workspace_id, *, name: str, kind: str):
    account = Account(
        workspace_id=workspace_id,
        display_name=name,
        normalized_name=f"{name.casefold()} {uuid4()}",
    )
    identity = SourceIdentity(
        workspace_id=workspace_id,
        source_system="gmail",
        source_scope="mailbox:test",
        entity_kind=kind,
        external_id=f"{kind}-{uuid4()}",
        metadata_json={},
    )
    session.add_all([account, identity])
    session.flush()
    return account, identity


@pytest.mark.parametrize("model_name", ["email", "meeting"])
def test_engagement_evidence_must_belong_to_the_same_account(engine, model_name):
    workspace_id = uuid4()
    occurred_at = datetime(2026, 7, 18, 15, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(id=workspace_id, slug=f"evidence-{workspace_id}", name="Evidence")
        )
        session.flush()
        engagement_account, mailbox = _account_with_identity(
            session, workspace_id, name="Engagement Account", kind="mailbox"
        )
        evidence_account, evidence_source = _account_with_identity(
            session, workspace_id, name="Evidence Account", kind="message"
        )
        evidence = Evidence(
            workspace_id=workspace_id,
            account_id=evidence_account.id,
            evidence_type="email_message" if model_name == "email" else "meeting_note",
            source_identity_id=evidence_source.id,
            content_hash="a" * 64,
            captured_at=occurred_at,
            metadata_json={},
        )
        session.add(evidence)
        session.flush()
        if model_name == "email":
            engagement = EmailMessage(
                workspace_id=workspace_id,
                account_id=engagement_account.id,
                mailbox_identity_id=mailbox.id,
                provider_message_id="cross-account-evidence",
                provider_thread_id="thread",
                direction="outbound",
                sent_at=occurred_at,
                evidence_id=evidence.id,
            )
        else:
            engagement = Meeting(
                workspace_id=workspace_id,
                account_id=engagement_account.id,
                provider="google_calendar",
                calendar_id="commercial",
                external_event_id="cross-account-evidence",
                occurrence_start_at=occurred_at,
                scheduled_start_at=occurred_at,
                status="booked",
                notes_evidence_id=evidence.id,
            )
        session.add(engagement)
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.parametrize(
    "model_name,evidence_type",
    [("email", "payment"), ("meeting", "contract")],
)
def test_engagement_rejects_wrong_evidence_semantics(engine, model_name, evidence_type):
    workspace_id = uuid4()
    occurred_at = datetime(2026, 7, 18, 15, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id, slug=f"typed-evidence-{workspace_id}", name="Typed"
            )
        )
        session.flush()
        account, mailbox = _account_with_identity(
            session, workspace_id, name="Typed Evidence", kind="mailbox"
        )
        evidence = Evidence(
            workspace_id=workspace_id,
            account_id=account.id,
            evidence_type=evidence_type,
            source_identity_id=mailbox.id,
            content_hash="b" * 64,
            captured_at=occurred_at,
            metadata_json={},
        )
        session.add(evidence)
        session.flush()
        if model_name == "email":
            engagement = EmailMessage(
                workspace_id=workspace_id,
                account_id=account.id,
                mailbox_identity_id=mailbox.id,
                provider_message_id="wrong-evidence-type",
                provider_thread_id="thread",
                direction="outbound",
                sent_at=occurred_at,
                evidence_id=evidence.id,
            )
        else:
            engagement = Meeting(
                workspace_id=workspace_id,
                account_id=account.id,
                provider="google_calendar",
                calendar_id="commercial",
                external_event_id="wrong-evidence-type",
                occurrence_start_at=occurred_at,
                scheduled_start_at=occurred_at,
                status="booked",
                notes_evidence_id=evidence.id,
            )
        session.add(engagement)
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.parametrize(
    "source_system,entity_kind", [("gmail", "thread"), ("manual", "message")]
)
def test_email_message_rejects_non_mailbox_source_identity(
    engine, source_system, entity_kind
):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(id=workspace_id, slug=f"mailbox-{workspace_id}", name="Mailbox")
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Mailbox Account",
            normalized_name=f"mailbox account {workspace_id}",
        )
        identity = SourceIdentity(
            workspace_id=workspace_id,
            source_system=source_system,
            source_scope="mailbox:test",
            entity_kind=entity_kind,
            external_id=f"identity-{uuid4()}",
            metadata_json={},
        )
        session.add_all([account, identity])
        session.flush()
        session.add(
            EmailMessage(
                workspace_id=workspace_id,
                account_id=account.id,
                mailbox_identity_id=identity.id,
                provider_message_id="wrong-mailbox-semantics",
                provider_thread_id="thread",
                direction="outbound",
                sent_at=datetime(2026, 7, 18, tzinfo=UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.parametrize(
    "field,value",
    [
        ("to_addresses", {"raw": "private"}),
        ("to_addresses", ["x" * 321]),
        ("to_addresses", ["   "]),
        ("subject", "x" * 513),
        ("body_preview_redacted", "x" * 2049),
    ],
)
def test_email_message_rejects_unbounded_or_unstructured_content(engine, field, value):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id, slug=f"bounded-mail-{workspace_id}", name="Bounded"
            )
        )
        session.flush()
        account, mailbox = _account_with_identity(
            session, workspace_id, name="Bounded Mail", kind="mailbox"
        )
        values = {
            "workspace_id": workspace_id,
            "account_id": account.id,
            "mailbox_identity_id": mailbox.id,
            "provider_message_id": f"bounded-{field}",
            "provider_thread_id": "thread",
            "direction": "outbound",
            "sent_at": datetime(2026, 7, 18, tzinfo=UTC),
        }
        values[field] = value
        session.add(EmailMessage(**values))
        with pytest.raises((IntegrityError, TypeError)):
            session.flush()


def test_task_rejects_unbounded_title(engine):
    workspace_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id, slug=f"bounded-task-{workspace_id}", name="Bounded"
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Bounded Task",
            normalized_name=f"bounded task {workspace_id}",
        )
        session.add(account)
        session.flush()
        session.add(
            Task(
                workspace_id=workspace_id,
                account_id=account.id,
                task_type="follow_up",
                title="x" * 513,
                due_at=datetime(2026, 7, 19, tzinfo=UTC),
                owner_user_id=uuid4(),
                status="open",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_meeting_rejects_provider_shaped_next_steps(engine):
    workspace_id = uuid4()
    occurred_at = datetime(2026, 7, 18, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id, slug=f"bounded-meeting-{workspace_id}", name="Bounded"
            )
        )
        session.flush()
        account = Account(
            workspace_id=workspace_id,
            display_name="Bounded Meeting",
            normalized_name=f"bounded meeting {workspace_id}",
        )
        session.add(account)
        session.flush()
        session.add(
            Meeting(
                workspace_id=workspace_id,
                account_id=account.id,
                provider="google_calendar",
                calendar_id="commercial",
                external_event_id="bounded-next-steps",
                occurrence_start_at=occurred_at,
                scheduled_start_at=occurred_at,
                status="booked",
                next_steps={"provider_payload": {"raw": "private"}},
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
