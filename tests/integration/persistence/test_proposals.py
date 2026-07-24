from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from src.crm.persistence.models import (
    Account,
    Activity,
    Evidence,
    Proposal,
    ProposalFollowup,
    ProposalItem,
    ProposalVersion,
    SourceIdentity,
    Workspace,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.proposal_service import (
    AppendProposalVersionCommand,
    CreateProposalCommand,
    ProposalConflictError,
    ProposalService,
)
from tests.migration._postgres import require_disposable_postgres

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "migrations/alembic.ini"


@pytest.fixture(scope="module")
def engine():
    url = require_disposable_postgres()

    def run_alembic(*arguments: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(CONFIG), *arguments],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
        )

    result = run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    value = create_engine(url)
    try:
        yield value
    finally:
        value.dispose()
        result = run_alembic("downgrade", "base")
        assert result.returncode == 0, result.stderr
        result = run_alembic("upgrade", "head")
        assert result.returncode == 0, result.stderr


def _preserve_until_schema_disposal(_engine, _workspace_id) -> None:
    """Append-only proposal graphs are disposed with the test schema."""


def _add_document_evidence(session, workspace_id, account_id):
    source = SourceIdentity(
        workspace_id=workspace_id,
        source_system="manual",
        entity_kind="document",
        source_scope="test",
        external_id=f"document:{uuid4()}",
    )
    session.add(source)
    session.flush()
    evidence = Evidence(
        workspace_id=workspace_id,
        account_id=account_id,
        source_identity_id=source.id,
        evidence_type="manual_confirmation",
        content_hash=uuid4().hex + uuid4().hex,
        captured_at=datetime.now(UTC),
    )
    session.add(evidence)
    session.flush()
    return evidence.id


def test_proposal_migration_creates_separate_versioned_portfolio_tables(engine):
    inspector = inspect(engine)

    assert {
        "proposals",
        "proposal_versions",
        "proposal_items",
        "proposal_followups",
    } <= set(inspector.get_table_names())

    proposal_columns = {
        column["name"]: column for column in inspector.get_columns("proposals")
    }
    assert proposal_columns["account_id"]["nullable"] is False
    assert proposal_columns["lead_id"]["nullable"] is True
    assert proposal_columns["probability"]["nullable"] is True
    assert proposal_columns["selected_version_id"]["nullable"] is True
    assert proposal_columns["value_state"]["nullable"] is False

    version_columns = {
        column["name"]: column for column in inspector.get_columns("proposal_versions")
    }
    for name in ("one_off_amount", "mrr_amount", "arr_amount"):
        assert version_columns[name]["nullable"] is True
    assert str(version_columns["valid_until"]["type"]) == "DATE"

    item_columns = {
        column["name"]: column for column in inspector.get_columns("proposal_items")
    }
    assert item_columns["quantity"]["nullable"] is True

    version_uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("proposal_versions")
    }
    assert "uq_proposal_versions_proposal_version_number" in version_uniques
    assert "ck_proposal_versions_sent_at" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("proposal_versions")
    }


def test_proposal_constraints_preserve_unknown_values_and_tenant_ownership(engine):
    inspector = inspect(engine)

    proposal_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("proposals")
    }
    assert {
        "ck_proposals_status",
        "ck_proposals_currency_iso",
        "ck_proposals_probability",
        "ck_proposals_value_state",
        "ck_proposals_sent_evidence",
    } <= proposal_checks

    proposal_fks = {
        constraint["name"] for constraint in inspector.get_foreign_keys("proposals")
    }
    assert {
        "fk_proposals_workspace_id_workspaces",
        "fk_proposals_workspace_account",
        "fk_proposals_workspace_account_lead",
        "fk_proposals_selected_version",
    } <= proposal_fks

    item_indexes = {
        index["name"]: index for index in inspector.get_indexes("proposal_items")
    }
    assert item_indexes["uq_proposal_items_selected_option"]["unique"] is True
    item_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("proposal_items")
    }
    assert "ck_proposal_items_billing_period" in item_checks
    orm_item_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in ProposalItem.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert orm_item_checks["ck_proposal_items_billing_period"] == (
        "billing_period IS NULL OR billing_period IN ('mrr', 'arr')"
    )

    followup_uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("proposal_followups")
    }
    assert {
        "uq_proposal_followups_activity",
        "uq_proposal_followups_sequence",
    } <= followup_uniques


def _add_workspace_account(session: Session, label: str):
    workspace = Workspace(slug=f"{label}-{uuid4()}", name=label)
    session.add(workspace)
    session.flush()
    account = Account(
        workspace_id=workspace.id,
        display_name=f"{label} account",
        normalized_name=f"{label.lower()} account",
    )
    session.add(account)
    session.flush()
    return workspace.id, account.id


def test_unknown_and_confirmed_zero_values_persist_without_synthetic_defaults(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Money")
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Money proposal",
            currency="EUR",
            value_state="missing",
        )
        session.add(proposal)
        session.flush()
        missing = ProposalVersion(proposal_id=proposal.id, version_number=1)
        zero = ProposalVersion(
            proposal_id=proposal.id,
            version_number=2,
            one_off_amount=Decimal("0.00"),
            source_document_evidence_id=_add_document_evidence(
                session, workspace_id, account_id
            ),
            confirmed_by=uuid4(),
            confirmed_at=datetime.now(UTC),
        )
        session.add_all([missing, zero])
        session.flush()
        proposal.selected_version_id = zero.id
        proposal.value_state = "confirmed"
        session.add(
            ProposalItem(
                proposal_version_id=zero.id,
                description="Unknown quantity option",
                quantity=None,
                amount=Decimal("0.00"),
                currency="EUR",
                option_group="hosting",
                is_selected=True,
            )
        )
        proposal_id = proposal.id
        zero_id = zero.id

    try:
        with Session(engine) as session:
            rows = list(
                session.scalars(
                    select(ProposalVersion)
                    .where(ProposalVersion.proposal_id == proposal_id)
                    .order_by(ProposalVersion.version_number)
                )
            )
            assert rows[0].one_off_amount is None
            assert rows[1].one_off_amount == Decimal("0.00")
            item = session.scalar(
                select(ProposalItem).where(
                    ProposalItem.proposal_version_id == rows[1].id
                )
            )
            assert item is not None and item.quantity is None

        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                ProposalItem(
                    proposal_version_id=zero_id,
                    description="Second selected option",
                    quantity=Decimal("1"),
                    currency="EUR",
                    option_group="hosting",
                    is_selected=True,
                )
            )
            session.flush()
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


def test_database_rejects_confirmation_without_any_amount(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(
            session, "Valueless confirmation"
        )
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Valueless confirmation proposal",
            currency="EUR",
        )
        session.add(proposal)
        session.flush()
        proposal_id = proposal.id

    try:
        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                ProposalVersion(
                    proposal_id=proposal_id,
                    version_number=1,
                    source_document_evidence_id=uuid4(),
                    confirmed_by=uuid4(),
                    confirmed_at=datetime.now(UTC),
                )
            )
            session.flush()
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


def test_database_rejects_item_currency_mismatch_and_unknown_billing_dimension(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Item dimensions")
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Dimension proposal",
            currency="EUR",
        )
        session.add(proposal)
        session.flush()
        version = ProposalVersion(proposal_id=proposal.id, version_number=1)
        session.add(version)
        session.flush()
        version_id = version.id

    try:
        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                ProposalItem(
                    proposal_version_id=version_id,
                    description="Wrong currency",
                    amount=Decimal("1.00"),
                    currency="USD",
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                ProposalItem(
                    proposal_version_id=version_id,
                    description="Unknown period",
                    amount=Decimal("1.00"),
                    currency="EUR",
                    billing_period="weekly",
                )
            )
            session.flush()
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


@pytest.mark.parametrize(
    ("updated_currency", "expected_outcomes"),
    [
        ("EUR", ["committed", "committed"]),
        ("USD", ["committed", "rejected"]),
    ],
)
def test_concurrent_item_insert_and_proposal_currency_update_serialize_without_deadlock(
    engine, updated_currency, expected_outcomes
):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Currency race")
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Currency race proposal",
            currency="EUR",
        )
        session.add(proposal)
        session.flush()
        version = ProposalVersion(proposal_id=proposal.id, version_number=1)
        session.add(version)
        session.flush()
        proposal_id, version_id = proposal.id, version.id

    proposal_row_locked = Event()
    item_advisory_locked = Event()
    proposal_update_started = Event()
    item_id = uuid4()

    def outcome(error: DBAPIError) -> str:
        sqlstate = error.orig.sqlstate
        if sqlstate == "40P01":
            return "deadlock"
        if sqlstate == "23514":
            return "rejected"
        raise error

    def update_proposal_currency() -> str:
        try:
            with engine.begin() as connection:
                connection.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection.execute(
                    text("SELECT id FROM proposals WHERE id = :id FOR UPDATE"),
                    {"id": proposal_id},
                )
                proposal_row_locked.set()
                assert item_advisory_locked.wait(timeout=5)
                proposal_update_started.set()
                connection.execute(
                    text("UPDATE proposals SET currency = :currency WHERE id = :id"),
                    {"currency": updated_currency, "id": proposal_id},
                )
            return "committed"
        except DBAPIError as error:
            return outcome(error)

    def insert_item() -> str:
        assert proposal_row_locked.wait(timeout=5)
        try:
            with engine.begin() as connection:
                connection.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended('crm-proposal:' || CAST(:id AS text), 0))"
                    ),
                    {"id": proposal_id},
                )
                item_advisory_locked.set()
                assert proposal_update_started.wait(timeout=5)
                connection.execute(
                    text(
                        "INSERT INTO proposal_items "
                        "(id, proposal_version_id, description, currency) "
                        "VALUES (:id, :version_id, 'Concurrent item', 'EUR')"
                    ),
                    {"id": item_id, "version_id": version_id},
                )
            return "committed"
        except DBAPIError as error:
            return outcome(error)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(update_proposal_currency),
                executor.submit(insert_item),
            )
            outcomes = sorted(future.result(timeout=10) for future in futures)

        assert outcomes == expected_outcomes
        with Session(engine) as session:
            persisted_proposal = session.get(Proposal, proposal_id)
            persisted_item = session.get(ProposalItem, item_id)
            assert persisted_proposal is not None
            assert persisted_item is not None
            assert persisted_item.currency == persisted_proposal.currency
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


def test_database_rejects_cross_tenant_account_and_foreign_selected_version(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Local")
        other_workspace_id, other_account_id = _add_workspace_account(session, "Other")

    try:
        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                Proposal(
                    workspace_id=workspace_id,
                    account_id=other_account_id,
                    title="Cross tenant",
                    currency="EUR",
                )
            )
            session.flush()

        with Session(engine) as session, session.begin():
            first = Proposal(
                workspace_id=workspace_id,
                account_id=account_id,
                title="First",
                currency="EUR",
            )
            second = Proposal(
                workspace_id=workspace_id,
                account_id=account_id,
                title="Second",
                currency="EUR",
            )
            session.add_all([first, second])
            session.flush()
            foreign_version = ProposalVersion(proposal_id=second.id, version_number=1)
            session.add(foreign_version)
            session.flush()
            ids = first.id, foreign_version.id

        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            row = session.get(Proposal, ids[0])
            assert row is not None
            row.selected_version_id = ids[1]
            session.flush()
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)
        _preserve_until_schema_disposal(engine, other_workspace_id)


def test_sent_evidence_version_and_followup_context_are_database_enforced(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Evidence")
        other = Account(
            workspace_id=workspace_id,
            display_name="Other account",
            normalized_name="other account",
        )
        session.add(other)
        session.flush()
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Evidence proposal",
            currency="EUR",
        )
        activity = Activity(
            workspace_id=workspace_id,
            account_id=other.id,
            activity_type="email_sent",
            occurred_at=datetime.now(UTC),
            title="Foreign account email",
        )
        session.add_all([proposal, activity])
        session.flush()
        ids = proposal.id, activity.id

    try:
        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            row = session.get(Proposal, ids[0])
            assert row is not None
            row.value_state = "confirmed"
            session.flush()

        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                Proposal(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    title="Unverified sent",
                    currency="EUR",
                    status="sent",
                    sent_at=datetime.now(UTC),
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                ProposalVersion(
                    proposal_id=ids[0],
                    version_number=1,
                    one_off_amount=Decimal("0.00"),
                    confirmed_by=uuid4(),
                    confirmed_at=datetime.now(UTC),
                )
            )
            session.flush()

        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                ProposalVersion(proposal_id=ids[0], version_number=1, status="sent")
            )
            session.flush()

        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                ProposalFollowup(
                    proposal_id=ids[0],
                    activity_id=ids[1],
                    sequence_number=1,
                    occurred_at=datetime.now(UTC),
                    channel="email",
                )
            )
            session.flush()
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


def test_concurrent_confirmation_and_supersession_cannot_commit_invalid_value_state(
    engine,
):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Value race")
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Value race proposal",
            currency="EUR",
            value_state="candidate",
        )
        session.add(proposal)
        session.flush()
        version = ProposalVersion(
            proposal_id=proposal.id,
            version_number=1,
            status="sent",
            sent_at=datetime.now(UTC),
            one_off_amount=Decimal("10.00"),
            source_document_evidence_id=_add_document_evidence(
                session, workspace_id, account_id
            ),
            confirmed_by=uuid4(),
            confirmed_at=datetime.now(UTC),
        )
        session.add(version)
        session.flush()
        proposal.selected_version_id = version.id
        proposal_id, version_id = proposal.id, version.id

    proposal_flushed = Event()
    version_attempted = Event()
    version_flushed = Event()
    proposal_validated = Event()
    version_validated = Event()
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def confirm() -> str:
        try:
            with factory.begin() as session:
                row = session.get(Proposal, proposal_id)
                assert row is not None
                row.value_state = "confirmed"
                session.flush()
                proposal_flushed.set()
                assert version_attempted.wait(timeout=5)
                version_was_unlocked = version_flushed.wait(timeout=1)
                session.execute(
                    text(
                        "SET CONSTRAINTS "
                        "trg_crm_proposals_validate_confirmed_value IMMEDIATE"
                    )
                )
                proposal_validated.set()
                if version_was_unlocked:
                    assert version_validated.wait(timeout=5)
            return "committed"
        except (IntegrityError, ProgrammingError):
            return "rejected"

    def supersede() -> str:
        assert proposal_flushed.wait(timeout=5)
        try:
            with factory.begin() as session:
                row = session.get(ProposalVersion, version_id)
                assert row is not None
                row.status = "superseded"
                version_attempted.set()
                session.flush()
                version_flushed.set()
                session.execute(
                    text(
                        "SET CONSTRAINTS "
                        "trg_crm_proposal_versions_validate_confirmed_value IMMEDIATE"
                    )
                )
                version_validated.set()
                assert proposal_validated.wait(timeout=5)
            return "committed"
        except (IntegrityError, ProgrammingError):
            return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(confirm), executor.submit(supersede))
            outcomes = sorted(future.result(timeout=10) for future in futures)
        assert outcomes == ["committed", "rejected"]
        with Session(engine) as session:
            proposal = session.get(Proposal, proposal_id)
            version = session.get(ProposalVersion, version_id)
            assert proposal is not None and proposal.value_state == "confirmed"
            assert version is not None and version.status == "sent"
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


def test_concurrent_account_change_and_followup_insert_cannot_cross_accounts(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Follow-up race")
        other = Account(
            workspace_id=workspace_id,
            display_name="New account",
            normalized_name="new account",
        )
        session.add(other)
        session.flush()
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Follow-up race proposal",
            currency="EUR",
        )
        activity = Activity(
            workspace_id=workspace_id,
            account_id=account_id,
            activity_type="email_sent",
            occurred_at=datetime.now(UTC),
            title="Original account follow-up",
        )
        session.add_all([proposal, activity])
        session.flush()
        proposal_id, activity_id, other_account_id = proposal.id, activity.id, other.id

    proposal_flushed = Event()
    followup_attempted = Event()
    followup_flushed = Event()
    followup_id = uuid4()
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def move_proposal() -> str:
        try:
            with factory.begin() as session:
                row = session.get(Proposal, proposal_id)
                assert row is not None
                row.account_id = other_account_id
                session.flush()
                proposal_flushed.set()
                assert followup_attempted.wait(timeout=5)
                followup_flushed.wait(timeout=1)
            return "committed"
        except IntegrityError:
            return "rejected"

    def add_followup() -> str:
        assert proposal_flushed.wait(timeout=5)
        try:
            with factory.begin() as session:
                session.add(
                    ProposalFollowup(
                        id=followup_id,
                        proposal_id=proposal_id,
                        activity_id=activity_id,
                        sequence_number=1,
                        occurred_at=datetime.now(UTC),
                        channel="email",
                    )
                )
                followup_attempted.set()
                session.flush()
                followup_flushed.set()
            return "committed"
        except IntegrityError:
            return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(move_proposal), executor.submit(add_followup))
            outcomes = sorted(future.result(timeout=10) for future in futures)
        assert outcomes == ["committed", "rejected"]
        with Session(engine) as session:
            proposal = session.get(Proposal, proposal_id)
            assert proposal is not None and proposal.account_id == other_account_id
            assert session.get(ProposalFollowup, followup_id) is None
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


def test_proposal_versions_reject_value_updates_and_deletes_but_allow_supersession(
    engine,
):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Immutable")
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Immutable history",
            currency="EUR",
        )
        session.add(proposal)
        session.flush()
        version = ProposalVersion(
            proposal_id=proposal.id,
            version_number=1,
            one_off_amount=Decimal("10.00"),
        )
        session.add(version)
        session.flush()
        version_id = version.id

    with pytest.raises(ProgrammingError), Session(engine) as session, session.begin():
        row = session.get(ProposalVersion, version_id)
        assert row is not None
        row.one_off_amount = Decimal("20.00")
        session.flush()

    with pytest.raises(ProgrammingError), Session(engine) as session, session.begin():
        row = session.get(ProposalVersion, version_id)
        assert row is not None
        session.delete(row)
        session.flush()

    with Session(engine) as session, session.begin():
        row = session.get(ProposalVersion, version_id)
        assert row is not None
        row.status = "superseded"
        session.flush()


def test_concurrent_version_appends_are_serialized_and_monotonic(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Concurrent")
        proposal = Proposal(
            workspace_id=workspace_id,
            account_id=account_id,
            title="Concurrent versions",
            currency="EUR",
        )
        session.add(proposal)
        session.flush()
        proposal_id = proposal.id

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def append(amount: str) -> tuple[str, int | None]:
        try:
            with SqlAlchemyUnitOfWork(factory) as uow:
                row = ProposalService(uow).append_version(
                    AppendProposalVersionCommand(
                        workspace_id=workspace_id,
                        proposal_id=proposal_id,
                        expected_version=1,
                        one_off_amount=Decimal(amount),
                    )
                )
                number = row.version_number
                uow.commit()
                return "appended", number
        except ProposalConflictError:
            return "conflict", None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(append, ("10.00", "20.00")))
        assert sorted(status for status, _ in results) == ["appended", "conflict"]

        with SqlAlchemyUnitOfWork(factory) as uow:
            retried = ProposalService(uow).append_version(
                AppendProposalVersionCommand(
                    workspace_id=workspace_id,
                    proposal_id=proposal_id,
                    expected_version=2,
                    one_off_amount=Decimal("30.00"),
                )
            )
            assert retried.version_number == 2
            uow.commit()

        with Session(engine) as session:
            persisted = list(
                session.scalars(
                    select(ProposalVersion)
                    .where(ProposalVersion.proposal_id == proposal_id)
                    .order_by(ProposalVersion.version_number)
                )
            )
            assert [row.version_number for row in persisted] == [1, 2]
            assert persisted[0].one_off_amount in {
                Decimal("10.00"),
                Decimal("20.00"),
            }
            assert persisted[1].one_off_amount == Decimal("30.00")
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)


def test_proposal_service_leaves_rollback_and_commit_to_uow_caller(engine):
    with Session(engine) as session, session.begin():
        workspace_id, account_id = _add_workspace_account(session, "Rollback")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with SqlAlchemyUnitOfWork(factory) as uow:
            created = ProposalService(uow).create_proposal(
                CreateProposalCommand(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    title="Rolled back",
                    currency="EUR",
                )
            )
            rolled_back_id = created.id

        with Session(engine) as session:
            assert session.get(Proposal, rolled_back_id) is None

        with SqlAlchemyUnitOfWork(factory) as uow:
            created = ProposalService(uow).create_proposal(
                CreateProposalCommand(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    title="Committed",
                    currency="EUR",
                )
            )
            committed_id = created.id
            uow.commit()

        with Session(engine) as session:
            assert session.get(Proposal, committed_id) is not None
    finally:
        _preserve_until_schema_disposal(engine, workspace_id)
