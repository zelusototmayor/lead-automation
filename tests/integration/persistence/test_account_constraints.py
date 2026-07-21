from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, BrokenBarrierError
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    IngestEvent,
    Lead,
    SourceIdentity,
    Workspace,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.account_service import (
    AccountService,
    IdentityHints,
    IdentityReviewRequired,
    ReplayConflictError,
    StageTransitionCommand,
)
from src.crm.services.activity_service import ActivityService, AppendActivityCommand


ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "migrations/alembic.ini"
NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)


def database_url():
    value = os.getenv("DATABASE_URL")
    if not value or not value.startswith("postgresql+psycopg://"):
        pytest.skip("requires disposable PostgreSQL")
    return value


@pytest.fixture(scope="module")
def engine():
    url = database_url()
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(CONFIG), "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    value = create_engine(url)
    yield value
    value.dispose()


@pytest.fixture(autouse=True)
def clean(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE activities, contacts, leads, accounts, source_identities, ingest_events, sync_checkpoints, workspaces CASCADE"
            )
        )


@pytest.fixture
def workspace(engine):
    with Session(engine) as session, session.begin():
        row = Workspace(slug=f"w-{uuid4().hex}", name="Workspace")
        session.add(row)
        session.flush()
        return row.id


def make_account(workspace_id, **kw):
    values = {"display_name": "Acme", "normalized_name": "acme", **kw}
    return Account(workspace_id=workspace_id, **values)


def create_ingest_event(engine, workspace_id, event_id):
    with Session(engine) as session, session.begin():
        session.add(
            IngestEvent(
                id=event_id,
                workspace_id=workspace_id,
                source_system="manual",
                source_scope="task6",
                event_type="stage.transition",
                schema_version=1,
                idempotency_key=str(event_id),
                occurred_at=NOW,
                payload={},
                payload_hash="0" * 64,
            )
        )


def test_migration_has_expected_schema_constraints(engine):
    inspector = inspect(engine)
    assert {"accounts", "contacts", "leads", "activities"} <= set(
        inspector.get_table_names()
    )
    lead_checks = {c["name"] for c in inspector.get_check_constraints("leads")}
    assert {
        "ck_leads_stage_requires_account",
        "ck_leads_contact_requires_account",
        "ck_leads_source_stage_raw_nonblank",
        "ck_leads_priority_nonblank",
        "ck_leads_sector_nonblank",
        "ck_leads_vertical_nonblank",
        "ck_leads_source_origin_nonblank",
    } <= lead_checks
    contact_indexes = {i["name"]: i for i in inspector.get_indexes("contacts")}
    assert contact_indexes["uq_contacts_workspace_primary_email"]["unique"]
    assert (
        "primary_email IS NOT NULL"
        in contact_indexes["uq_contacts_workspace_primary_email"]["dialect_options"][
            "postgresql_where"
        ]
    )
    activity_indexes = {i["name"]: i for i in inspector.get_indexes("activities")}
    assert activity_indexes["uq_activities_workspace_ingest_type"]["unique"]
    activity_columns = {c["name"]: c for c in inspector.get_columns("activities")}
    assert activity_columns["account_id"]["nullable"] is True
    assert activity_columns["semantic_fingerprint"]["nullable"] is True
    ingest_columns = {c["name"]: c for c in inspector.get_columns("ingest_events")}
    assert ingest_columns["stage_reduction_fingerprint"]["nullable"] is True
    assert "ck_ingest_events_stage_reduction_fingerprint" in {
        c["name"] for c in inspector.get_check_constraints("ingest_events")
    }
    expected_foreign_keys = {
        "accounts": {
            "fk_accounts_workspace_id_workspaces",
            "fk_accounts_workspace_merged_account",
            "fk_accounts_workspace_source_identity",
        },
        "contacts": {
            "fk_contacts_workspace_id_workspaces",
            "fk_contacts_workspace_account",
        },
        "leads": {
            "fk_leads_workspace_id_workspaces",
            "fk_leads_workspace_account",
            "fk_leads_workspace_source_identity",
            "fk_leads_workspace_account_contact",
        },
        "activities": {
            "fk_activities_workspace_id_workspaces",
            "fk_activities_workspace_account",
            "fk_activities_workspace_account_lead",
            "fk_activities_workspace_lead",
            "fk_activities_workspace_account_contact",
            "fk_activities_workspace_source_identity",
            "fk_activities_workspace_ingest_event",
            "fk_activities_workspace_account_supersedes",
            "fk_activities_workspace_supersedes",
        },
    }
    for table, names in expected_foreign_keys.items():
        assert {item["name"] for item in inspector.get_foreign_keys(table)} == names
    assert {
        "ck_activities_direction",
        "ck_activities_source_system",
        "ck_activities_summary_nonblank",
        "ck_activities_actor_type_nonblank",
        "ck_activities_not_self_superseding",
        "ck_activities_requires_entity",
        "ck_activities_contact_requires_account",
        "ck_activities_semantic_fingerprint",
    } <= {c["name"] for c in inspector.get_check_constraints("activities")}
    assert "uq_activities_workspace_id" in {
        c["name"] for c in inspector.get_unique_constraints("activities")
    }


def test_accountless_activity_database_entity_and_tenant_constraints(engine, workspace):
    other = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=other, slug=f"w-{other.hex}", name="Other"))
        account = make_account(workspace)
        session.add(account)
        session.flush()
        local_lead = Lead(workspace_id=workspace)
        foreign_lead = Lead(workspace_id=other)
        contact = Contact(workspace_id=workspace, account_id=account.id)
        session.add_all([local_lead, foreign_lead, contact])
        session.flush()
        ids = local_lead.id, foreign_lead.id, contact.id, account.id

    local_lead_id, foreign_lead_id, contact_id, account_id = ids
    with Session(engine) as session, session.begin():
        session.add(
            Activity(
                workspace_id=workspace,
                lead_id=local_lead_id,
                activity_type="note",
                occurred_at=NOW,
                title="Accountless",
            )
        )
        session.flush()

    invalid = (
        Activity(
            workspace_id=workspace,
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        Activity(
            workspace_id=workspace,
            lead_id=foreign_lead_id,
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        Activity(
            workspace_id=workspace,
            contact_id=contact_id,
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        Activity(
            workspace_id=workspace,
            account_id=account_id,
            lead_id=local_lead_id,
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        Activity(
            workspace_id=workspace,
            lead_id=local_lead_id,
            activity_type="stage_change",
            occurred_at=NOW,
            title="Missing fingerprint",
        ),
        Activity(
            workspace_id=workspace,
            lead_id=local_lead_id,
            activity_type="stage_change",
            occurred_at=NOW,
            title="Uppercase fingerprint",
            semantic_fingerprint="A" * 64,
        ),
    )
    for row in invalid:
        with Session(engine) as session:
            session.add(row)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()


def test_supersedes_enforces_workspace_and_nullable_account_context(engine, workspace):
    other = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=other, slug=f"w-{other.hex}", name="Other"))
        session.flush()
        lead = Lead(workspace_id=workspace)
        other_lead = Lead(workspace_id=other)
        account = make_account(workspace)
        session.add_all([lead, other_lead, account])
        session.flush()
        originals = [
            Activity(
                workspace_id=workspace,
                lead_id=lead.id,
                activity_type="note",
                occurred_at=NOW,
                title="Local",
            ),
            Activity(
                workspace_id=other,
                lead_id=other_lead.id,
                activity_type="note",
                occurred_at=NOW,
                title="Other",
            ),
        ]
        session.add_all(originals)
        session.flush()
        lead_id, local_id, other_id, account_id = (
            lead.id,
            originals[0].id,
            originals[1].id,
            account.id,
        )

    with Session(engine) as session, session.begin():
        session.add(
            Activity(
                workspace_id=workspace,
                lead_id=lead_id,
                activity_type="note",
                occurred_at=NOW,
                title="Valid correction",
                supersedes_activity_id=local_id,
            )
        )
        session.flush()

    invalid = (
        Activity(
            workspace_id=workspace,
            account_id=account_id,
            activity_type="note",
            occurred_at=NOW,
            title="Wrong account context",
            supersedes_activity_id=local_id,
        ),
        Activity(
            workspace_id=workspace,
            lead_id=lead_id,
            activity_type="note",
            occurred_at=NOW,
            title="Cross workspace",
            supersedes_activity_id=other_id,
        ),
    )
    for row in invalid:
        with Session(engine) as session:
            session.add(row)
            with pytest.raises((IntegrityError, ProgrammingError)):
                session.flush()
            session.rollback()


def test_activity_context_trigger_closes_nullable_fk_bypasses(engine, workspace):
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        linked = Lead(workspace_id=workspace, account_id=account.id)
        accountless = Lead(workspace_id=workspace)
        session.add_all([linked, accountless])
        session.flush()
        accountful = Activity(
            workspace_id=workspace,
            account_id=account.id,
            lead_id=linked.id,
            activity_type="note",
            occurred_at=NOW,
            title="Accountful",
        )
        accountless_row = Activity(
            workspace_id=workspace,
            lead_id=accountless.id,
            activity_type="note",
            occurred_at=NOW,
            title="Accountless",
        )
        session.add_all([accountful, accountless_row])
        session.flush()
        ids = account.id, linked.id, accountless.id, accountful.id, accountless_row.id
    account_id, linked_id, accountless_id, accountful_id, accountless_activity_id = ids
    invalid = (
        Activity(
            workspace_id=workspace,
            lead_id=linked_id,
            activity_type="note",
            occurred_at=NOW,
            title="Missing account",
        ),
        Activity(
            workspace_id=workspace,
            account_id=account_id,
            lead_id=accountless_id,
            activity_type="note",
            occurred_at=NOW,
            title="Unexpected account",
        ),
        Activity(
            workspace_id=workspace,
            lead_id=accountless_id,
            supersedes_activity_id=accountful_id,
            activity_type="note",
            occurred_at=NOW,
            title="Drops context",
        ),
        Activity(
            workspace_id=workspace,
            account_id=account_id,
            supersedes_activity_id=accountless_activity_id,
            activity_type="note",
            occurred_at=NOW,
            title="Adds context",
        ),
    )
    for row in invalid:
        with Session(engine) as session:
            session.add(row)
            with pytest.raises(
                (IntegrityError, ProgrammingError), match="activity context mismatch"
            ):
                session.flush()
            session.rollback()
    with Session(engine) as session, session.begin():
        session.add(
            Activity(
                workspace_id=workspace,
                lead_id=accountless_id,
                supersedes_activity_id=accountless_activity_id,
                activity_type="note",
                occurred_at=NOW,
                title="Valid accountless chain",
            )
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda w: make_account(w, display_name=" "),
        lambda w: make_account(w, highest_stage_rank=91),
        lambda w: Contact(
            workspace_id=w, account_id=uuid4(), primary_email="x@example.invalid"
        ),
        lambda w: Lead(workspace_id=w, highest_stage_rank=-1),
        lambda w: Activity(
            workspace_id=w,
            account_id=uuid4(),
            activity_type="bad",
            occurred_at=NOW,
            title="x",
        ),
    ],
)
def test_checks_and_restricting_foreign_keys_reject_invalid_rows(
    engine, workspace, factory
):
    with Session(engine) as session, session.begin():
        session.add(factory(workspace))
        with pytest.raises(IntegrityError):
            session.flush()


def test_contact_exact_email_unique_per_workspace(engine, workspace):
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        session.add_all(
            [
                Contact(
                    workspace_id=workspace,
                    account_id=account.id,
                    primary_email="Person@Example.Invalid",
                ),
                Contact(
                    workspace_id=workspace,
                    account_id=account.id,
                    primary_email="person@example.invalid",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_optimistic_versions_reject_stale_account_update(engine, workspace):
    with Session(engine) as setup, setup.begin():
        account = make_account(workspace)
        setup.add(account)
        setup.flush()
        account_id = account.id
    first = Session(engine)
    second = Session(engine)
    try:
        one = first.get(Account, account_id)
        two = second.get(Account, account_id)
        one.display_name = "First"
        first.commit()
        two.display_name = "Second"
        with pytest.raises(StaleDataError):
            second.commit()
    finally:
        first.close()
        second.close()


def test_activity_is_immutable_in_orm_and_raw_sql_but_correction_insert_is_allowed(
    engine, workspace
):
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        activity = Activity(
            workspace_id=workspace,
            account_id=account.id,
            activity_type="note",
            occurred_at=NOW,
            title="Original",
        )
        session.add(activity)
        session.flush()
        activity_id = activity.id
        account_id = account.id
    with Session(engine) as session:
        row = session.get(Activity, activity_id)
        row.title = "Changed"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()
    with engine.begin() as connection:
        with pytest.raises(ProgrammingError):
            connection.execute(
                text("UPDATE activities SET title='Changed' WHERE id=:id"),
                {"id": activity_id},
            )
    with engine.begin() as connection:
        with pytest.raises(ProgrammingError):
            connection.execute(
                text("DELETE FROM activities WHERE id=:id"), {"id": activity_id}
            )
    with Session(engine) as session, session.begin():
        correction = Activity(
            workspace_id=workspace,
            account_id=account_id,
            activity_type="note",
            occurred_at=NOW,
            title="Correction",
            supersedes_activity_id=activity_id,
        )
        session.add(correction)
        session.flush()


def test_service_transition_is_atomic_and_replay_is_idempotent(engine, workspace):
    factory = sessionmaker(engine, expire_on_commit=False)
    service = AccountService(lambda: SqlAlchemyUnitOfWork(factory))
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    command = StageTransitionCommand(
        workspace_id=workspace,
        target_stage="proposal_sent",
        identity=IdentityHints(
            company_name="Acme",
            domain="acme.invalid",
            contact_email="person@example.invalid",
        ),
        occurred_at=NOW,
        ingest_event_id=event,
        commercial_classification="confirmed",
    )
    first = service.apply_stage_transition(command)
    second = service.apply_stage_transition(command)
    assert first == second
    with Session(engine) as session:
        assert len(session.scalars(select(Account)).all()) == 1
        assert len(session.scalars(select(Lead)).all()) == 1
        activities = session.scalars(select(Activity)).all()
        assert len(activities) == 1
        assert (activities[0].from_stage, activities[0].to_stage) == (
            "new",
            "proposal_sent",
        )
        lead = session.get(Lead, first.lead_id)
        assert lead.account_id == first.account_id and lead.stage == "proposal_sent"


def test_pg_accountless_contacted_event_applies_and_replays_with_one_activity(
    engine, workspace
):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    with Session(engine) as session, session.begin():
        lead = Lead(workspace_id=workspace)
        session.add(lead)
        session.flush()
        lead_id = lead.id
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    command = StageTransitionCommand(
        workspace_id=workspace,
        lead_id=lead_id,
        target_stage="contacted",
        identity=IdentityHints(),
        occurred_at=NOW,
        ingest_event_id=event,
    )
    first = service.apply_stage_transition(command)
    assert service.apply_stage_transition(command) == first
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        activities = session.scalars(select(Activity)).all()
        assert lead.account_id is None and lead.stage == "contacted"
        assert len(activities) == 1
        assert activities[0].account_id is None
        assert activities[0].lead_id == lead_id
        assert (activities[0].from_stage, activities[0].to_stage) == (
            "new",
            "contacted",
        )
        assert len(activities[0].semantic_fingerprint) == 64


def test_pg_replay_changed_entity_semantics_conflict_but_normalized_equivalent_succeeds(
    engine, workspace
):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )

    def transition(hints):
        return StageTransitionCommand(
            workspace_id=workspace,
            target_stage="meeting_booked",
            identity=hints,
            occurred_at=NOW,
            ingest_event_id=event,
        )

    first = service.apply_stage_transition(
        transition(
            IdentityHints(
                contact_email=" Person@Example.Invalid ",
                company_name=" AＣＭＥ ",
                domain="ACME.INVALID.",
            )
        )
    )
    assert (
        service.apply_stage_transition(
            transition(
                IdentityHints(
                    contact_email="person@example.invalid",
                    company_name="acme",
                    domain="acme.invalid",
                )
            )
        )
        == first
    )
    changed = (
        IdentityHints(
            account_id=uuid4(),
            contact_email="person@example.invalid",
            company_name="Acme",
            domain="acme.invalid",
        ),
        IdentityHints(
            source_identity_id=uuid4(),
            contact_email="person@example.invalid",
            company_name="Acme",
            domain="acme.invalid",
        ),
        IdentityHints(
            contact_email="other@example.invalid",
            company_name="Acme",
            domain="acme.invalid",
        ),
        IdentityHints(
            contact_email="person@example.invalid",
            company_name="Beta",
            domain="beta.invalid",
        ),
    )
    for hints in changed:
        with pytest.raises(ReplayConflictError, match="different semantics"):
            service.apply_stage_transition(transition(hints))


def test_source_identity_link_is_workspace_scoped_and_never_relinked(engine, workspace):
    other = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=other, slug=f"w-{uuid4().hex}", name="Other"))
        identity = SourceIdentity(
            workspace_id=workspace,
            source_system="manual",
            entity_kind="account",
            source_scope="test",
            external_id="source-1",
        )
        session.add(identity)
        session.flush()
        identity_id = identity.id
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    first = service.apply_stage_transition(
        StageTransitionCommand(
            workspace_id=workspace,
            target_stage="meeting_booked",
            identity=IdentityHints(company_name="Acme", source_identity_id=identity_id),
            occurred_at=NOW,
            commercial_classification="confirmed",
        )
    )
    with pytest.raises(IdentityReviewRequired):
        service.apply_stage_transition(
            StageTransitionCommand(
                workspace_id=other,
                target_stage="meeting_booked",
                identity=IdentityHints(
                    company_name="Other", source_identity_id=identity_id
                ),
                occurred_at=NOW,
                commercial_classification="confirmed",
            )
        )
    with Session(engine) as session:
        linked = session.get(SourceIdentity, identity_id)
        assert linked.canonical_entity_id == first.account_id


def test_concurrent_exact_source_first_identity_never_creates_duplicate_account(
    engine, workspace
):
    factory = sessionmaker(engine, expire_on_commit=False)
    events = [uuid4(), uuid4()]
    for event in events:
        create_ingest_event(engine, workspace, event)

    def apply(event):
        service = AccountService(lambda: SqlAlchemyUnitOfWork(factory))
        try:
            return service.apply_stage_transition(
                StageTransitionCommand(
                    workspace_id=workspace,
                    target_stage="meeting_booked",
                    identity=IdentityHints(
                        company_name="Concurrent",
                        domain="same.invalid",
                        contact_email="same@example.invalid",
                    ),
                    occurred_at=NOW,
                    ingest_event_id=event,
                    commercial_classification="confirmed",
                )
            )
        except IdentityReviewRequired:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, events))
    assert any(results)
    with Session(engine) as session:
        assert len(session.scalars(select(Account)).all()) == 1
        assert len(session.scalars(select(Lead)).all()) in (1, 2)


def test_existing_lead_and_source_first_shared_identity_do_not_deadlock(
    engine, workspace
):
    with Session(engine) as session, session.begin():
        account = make_account(
            workspace,
            display_name="Shared Company",
            normalized_name="shared company",
            primary_domain="shared.invalid",
        )
        session.add(account)
        session.flush()
        lead = Lead(workspace_id=workspace, account_id=account.id, stage="contacted")
        session.add(lead)
        session.flush()
        account_id, lead_id = account.id, lead.id

    first_locks = Barrier(2)
    factory = sessionmaker(engine, expire_on_commit=False)

    class CoordinatedUnitOfWork(SqlAlchemyUnitOfWork):
        def __init__(self, role):
            super().__init__(factory)
            self.role = role
            self.identity_locked = False

        def __enter__(self):
            uow = super().__enter__()
            assert self.session is not None
            self.session.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
            self.session.execute(text("SET LOCAL statement_timeout = '5s'"))
            original_get = self.accounts.get

            def coordinated_get(workspace_id, row_id, *, for_update=False):
                row = original_get(workspace_id, row_id, for_update=for_update)
                if (
                    self.role == "existing"
                    and for_update
                    and row_id == account_id
                    and not self.identity_locked
                ):
                    try:
                        first_locks.wait(timeout=1)
                    except BrokenBarrierError:
                        pass
                return row

            self.accounts.get = coordinated_get
            return uow

        def lock_identities(self, workspace_id, fingerprints):
            super().lock_identities(workspace_id, fingerprints)
            self.identity_locked = True
            if self.role == "source":
                try:
                    first_locks.wait(timeout=1)
                except BrokenBarrierError:
                    pass

    hints = IdentityHints(company_name="Shared Company", domain="shared.invalid")

    def existing_transition():
        return AccountService(
            lambda: CoordinatedUnitOfWork("existing")
        ).apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                lead_id=lead_id,
                target_stage="meeting_booked",
                identity=hints,
                occurred_at=NOW,
            )
        )

    def source_first_transition():
        return AccountService(
            lambda: CoordinatedUnitOfWork("source")
        ).apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                target_stage="meeting_booked",
                identity=hints,
                occurred_at=NOW,
                commercial_classification="confirmed",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        existing_future = pool.submit(existing_transition)
        source_future = pool.submit(source_first_transition)
        existing_result = existing_future.result(timeout=10)
        source_result = source_future.result(timeout=10)

    assert existing_result.account_id == source_result.account_id == account_id
    with Session(engine) as session:
        assert session.query(Account).count() == 1
        assert session.query(Lead).count() == 2
        assert session.query(Activity).count() == 2


def test_source_first_and_account_activity_lock_account_before_source_identity(
    engine, workspace
):
    with Session(engine) as session, session.begin():
        identity = SourceIdentity(
            workspace_id=workspace,
            source_system="manual",
            source_scope="task6",
            external_id=f"source-{uuid4()}",
            entity_kind="account",
        )
        session.add(identity)
        session.flush()
        account = make_account(
            workspace,
            display_name="Canonical Source",
            normalized_name="canonical source",
            source_identity_id=identity.id,
        )
        session.add(account)
        session.flush()
        identity.canonical_entity_type = "account"
        identity.canonical_entity_id = account.id
        account_id, identity_id = account.id, identity.id

    first_locks = Barrier(2)
    factory = sessionmaker(engine, expire_on_commit=False)

    class CoordinatedUnitOfWork(SqlAlchemyUnitOfWork):
        def __init__(self, role):
            super().__init__(factory)
            self.role = role

        def __enter__(self):
            uow = super().__enter__()
            assert self.session is not None
            self.session.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
            self.session.execute(text("SET LOCAL statement_timeout = '5s'"))
            if self.role == "activity":
                original_get = self.accounts.get

                def coordinated_get(workspace_id, row_id, *, for_update=False):
                    row = original_get(workspace_id, row_id, for_update=for_update)
                    if for_update and row_id == account_id:
                        try:
                            first_locks.wait(timeout=1)
                        except BrokenBarrierError:
                            pass
                    return row

                self.accounts.get = coordinated_get
            return uow

        def account_candidates(self, workspace_id, hints):
            candidates = super().account_candidates(workspace_id, hints)
            if self.role == "source":
                try:
                    first_locks.wait(timeout=1)
                except BrokenBarrierError:
                    pass
            return candidates

    def source_first_transition():
        service = AccountService(lambda: CoordinatedUnitOfWork("source"))
        return service.apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                target_stage="meeting_booked",
                identity=IdentityHints(
                    company_name="Canonical Source",
                    source_identity_id=identity_id,
                ),
                occurred_at=NOW,
                commercial_classification="confirmed",
            )
        )

    def append_account_note():
        return ActivityService(lambda: CoordinatedUnitOfWork("activity")).append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=account_id,
                source_identity_id=identity_id,
                activity_type="note",
                occurred_at=NOW,
                title="Concurrent account note",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        source_future = pool.submit(source_first_transition)
        note_future = pool.submit(append_account_note)
        source_result = source_future.result(timeout=10)
        note_result = note_future.result(timeout=10)

    with Session(engine) as session:
        lead = session.get(Lead, source_result.lead_id)
        activities = session.scalars(select(Activity)).all()
        assert lead.account_id == account_id and lead.source_identity_id == identity_id
        assert note_result.activity_id in {activity.id for activity in activities}
        assert {
            (activity.activity_type, activity.title) for activity in activities
        } == {
            ("stage_change", "Stage changed to meeting_booked"),
            ("note", "Concurrent account note"),
        }


def test_workspace_isolation_for_explicit_account(engine, workspace):
    other = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=other, slug=f"w-{uuid4().hex}", name="Other"))
        account = make_account(workspace)
        session.add(account)
        session.flush()
        account_id = account.id
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    with pytest.raises(IdentityReviewRequired):
        service.apply_stage_transition(
            StageTransitionCommand(
                workspace_id=other,
                target_stage="meeting_booked",
                identity=IdentityHints(account_id=account_id),
                occurred_at=NOW,
                commercial_classification="confirmed",
            )
        )


def _tenant_graph(engine):
    workspaces = (uuid4(), uuid4())
    with Session(engine) as session, session.begin():
        session.add_all(
            [Workspace(id=w, slug=f"w-{w.hex}", name=w.hex) for w in workspaces]
        )
        session.flush()
        sources = [
            SourceIdentity(
                workspace_id=w,
                source_system="manual",
                entity_kind="account",
                source_scope="tenant",
                external_id=w.hex,
            )
            for w in workspaces
        ]
        events = [
            IngestEvent(
                workspace_id=w,
                source_system="manual",
                source_scope="tenant",
                event_type="test",
                schema_version=1,
                idempotency_key=w.hex,
                occurred_at=NOW,
                payload={},
                payload_hash="0" * 64,
            )
            for w in workspaces
        ]
        accounts = [
            make_account(w, display_name=w.hex, normalized_name=w.hex)
            for w in workspaces
        ]
        session.add_all(sources + events + accounts)
        session.flush()
        contacts = [
            Contact(
                workspace_id=w,
                account_id=a.id,
                primary_email=f"{w.hex}@example.invalid",
            )
            for w, a in zip(workspaces, accounts, strict=True)
        ]
        session.add_all(contacts)
        session.flush()
        leads = [
            Lead(
                workspace_id=w,
                account_id=a.id,
                contact_id=c.id,
                source_identity_id=s.id,
            )
            for w, a, c, s in zip(workspaces, accounts, contacts, sources, strict=True)
        ]
        session.add_all(leads)
        session.flush()
        activities = [
            Activity(
                workspace_id=w,
                account_id=a.id,
                lead_id=lead_row.id,
                contact_id=c.id,
                source_identity_id=s.id,
                ingest_event_id=e.id,
                activity_type="note",
                occurred_at=NOW,
                title="Original",
            )
            for w, a, lead_row, c, s, e in zip(
                workspaces, accounts, leads, contacts, sources, events, strict=True
            )
        ]
        session.add_all(activities)
        session.flush()
        return {
            "w": workspaces,
            "source": tuple(x.id for x in sources),
            "event": tuple(x.id for x in events),
            "account": tuple(x.id for x in accounts),
            "contact": tuple(x.id for x in contacts),
            "lead": tuple(x.id for x in leads),
            "activity": tuple(x.id for x in activities),
        }


def test_every_composite_edge_rejects_cross_tenant_rows(engine):
    graph = _tenant_graph(engine)
    w1, _ = graph["w"]
    a1, a2 = graph["account"]
    factories = {
        "account merged": lambda: make_account(w1, merged_into_account_id=a2),
        "account source": lambda: make_account(
            w1, source_identity_id=graph["source"][1]
        ),
        "contact account": lambda: Contact(workspace_id=w1, account_id=a2),
        "lead account": lambda: Lead(workspace_id=w1, account_id=a2),
        "lead source": lambda: Lead(
            workspace_id=w1, source_identity_id=graph["source"][1]
        ),
        "lead contact-account": lambda: Lead(
            workspace_id=w1, account_id=a1, contact_id=graph["contact"][1]
        ),
        "activity account": lambda: Activity(
            workspace_id=w1,
            account_id=a2,
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        "activity lead": lambda: Activity(
            workspace_id=w1,
            account_id=a1,
            lead_id=graph["lead"][1],
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        "activity contact": lambda: Activity(
            workspace_id=w1,
            account_id=a1,
            contact_id=graph["contact"][1],
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        "activity source": lambda: Activity(
            workspace_id=w1,
            account_id=a1,
            source_identity_id=graph["source"][1],
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        "activity ingest": lambda: Activity(
            workspace_id=w1,
            account_id=a1,
            ingest_event_id=graph["event"][1],
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
        "activity supersedes": lambda: Activity(
            workspace_id=w1,
            account_id=a1,
            supersedes_activity_id=graph["activity"][1],
            activity_type="note",
            occurred_at=NOW,
            title="x",
        ),
    }
    with Session(engine) as session:
        before = {
            model: session.query(model).count()
            for model in (Account, Contact, Lead, Activity)
        }
    for edge, factory in factories.items():
        with Session(engine) as session:
            session.add(factory())
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
        with Session(engine) as session:
            assert {
                model: session.query(model).count() for model in before
            } == before, edge


def test_activity_service_pg_append_replay_conflict_and_correction(engine, workspace):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        contact = Contact(
            workspace_id=workspace,
            account_id=account.id,
            primary_email="activity@example.invalid",
        )
        session.add(contact)
        session.flush()
        lead = Lead(
            workspace_id=workspace, account_id=account.id, contact_id=contact.id
        )
        session.add(lead)
        session.flush()
        account_id, contact_id, lead_id = account.id, contact.id, lead.id
    service = ActivityService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    command = AppendActivityCommand(
        workspace_id=workspace,
        account_id=account_id,
        lead_id=lead_id,
        contact_id=contact_id,
        activity_type="note",
        occurred_at=NOW,
        title="Notes",
        summary="Line one\n\tLine two",
        direction="internal",
        source_system="manual",
        ingest_event_id=event,
        actor_type="agent",
    )
    first = service.append(command)
    assert service.append(command) == first
    with pytest.raises(ReplayConflictError):
        service.append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=account_id,
                lead_id=lead_id,
                contact_id=contact_id,
                activity_type="note",
                occurred_at=NOW,
                title="Different",
                ingest_event_id=event,
            )
        )
    correction = service.append(
        AppendActivityCommand(
            workspace_id=workspace,
            account_id=account_id,
            activity_type="note",
            occurred_at=NOW,
            title="Correction",
            supersedes_activity_id=first.activity_id,
        )
    )
    with Session(engine) as session:
        assert (
            session.get(Activity, correction.activity_id).supersedes_activity_id
            == first.activity_id
        )
        assert session.query(Activity).count() == 2


def test_activity_service_pg_concurrent_replay_is_serialized(engine, workspace):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        account_id = account.id
    factory = sessionmaker(engine, expire_on_commit=False)

    def append(title, event_id=event):
        return ActivityService(lambda: SqlAlchemyUnitOfWork(factory)).append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=account_id,
                activity_type=" note ",
                occurred_at=NOW,
                title=title,
                ingest_event_id=event_id,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        identical = list(pool.map(append, ("Same", "Same")))
    assert identical[0] == identical[1]
    with Session(engine) as session:
        assert session.query(Activity).count() == 1

    conflict_event = uuid4()
    create_ingest_event(engine, workspace, conflict_event)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(append, title, conflict_event) for title in ("One", "Two")
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ReplayConflictError as exc:
                outcomes.append(exc)
    assert sum(isinstance(item, ReplayConflictError) for item in outcomes) == 1
    with Session(engine) as session:
        assert session.query(Activity).count() == 2


def test_pg_stage_transition_and_activity_append_share_canonical_lock_order(
    engine, workspace
):
    stage_event, note_event = uuid4(), uuid4()
    create_ingest_event(engine, workspace, stage_event)
    create_ingest_event(engine, workspace, note_event)
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        lead = Lead(workspace_id=workspace, account_id=account.id)
        session.add(lead)
        session.flush()
        account_id, lead_id = account.id, lead.id

    first_locks = Barrier(2)
    factory = sessionmaker(engine, expire_on_commit=False)

    class CoordinatedUnitOfWork(SqlAlchemyUnitOfWork):
        def __init__(self, role):
            super().__init__(factory)
            self.role = role

        def __enter__(self):
            uow = super().__enter__()
            assert self.session is not None
            self.session.execute(text("SET LOCAL statement_timeout = '5s'"))
            repository = self.leads if self.role == "stage" else self.accounts
            expected_id = lead_id if self.role == "stage" else account_id
            original_get = repository.get

            def coordinated_get(workspace_id, row_id, *, for_update=False):
                row = original_get(workspace_id, row_id, for_update=for_update)
                if for_update and row_id == expected_id:
                    try:
                        first_locks.wait(timeout=2)
                    except BrokenBarrierError:
                        pass
                return row

            repository.get = coordinated_get
            return uow

    def transition():
        return AccountService(
            lambda: CoordinatedUnitOfWork("stage")
        ).apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                lead_id=lead_id,
                target_stage="proposal_sent",
                identity=IdentityHints(account_id=account_id),
                occurred_at=NOW,
                ingest_event_id=stage_event,
            )
        )

    def append_note():
        return ActivityService(lambda: CoordinatedUnitOfWork("activity")).append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=account_id,
                lead_id=lead_id,
                activity_type="note",
                occurred_at=NOW,
                title="Concurrent note",
                ingest_event_id=note_event,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stage_future = pool.submit(transition)
        note_future = pool.submit(append_note)
        stage_result = stage_future.result(timeout=10)
        note_result = note_future.result(timeout=10)

    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        activities = session.scalars(
            select(Activity).where(Activity.lead_id == lead_id)
        ).all()
        assert stage_result.lead_id == lead_id
        assert note_result.activity_id in {activity.id for activity in activities}
        assert lead.stage == "proposal_sent" and lead.highest_stage_rank == 70
        assert {
            (activity.activity_type, activity.title) for activity in activities
        } == {
            ("stage_change", "Stage changed to proposal_sent"),
            ("note", "Concurrent note"),
        }


def test_stage_and_note_with_same_ingest_event_lock_event_before_lead(
    engine, workspace
):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        lead = Lead(workspace_id=workspace, account_id=account.id, stage="contacted")
        session.add(lead)
        session.flush()
        account_id, lead_id = account.id, lead.id

    first_locks = Barrier(2)
    factory = sessionmaker(engine, expire_on_commit=False)

    class CoordinatedUnitOfWork(SqlAlchemyUnitOfWork):
        def __init__(self, role):
            super().__init__(factory)
            self.role = role

        def __enter__(self):
            uow = super().__enter__()
            assert self.session is not None
            self.session.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
            self.session.execute(text("SET LOCAL statement_timeout = '5s'"))
            if self.role == "activity":
                original_get = self.leads.get

                def coordinated_get(workspace_id, row_id, *, for_update=False):
                    row = original_get(workspace_id, row_id, for_update=for_update)
                    if for_update and row_id == lead_id:
                        try:
                            first_locks.wait(timeout=1)
                        except BrokenBarrierError:
                            pass
                    return row

                self.leads.get = coordinated_get
            return uow

        def claim_stage_reduction(self, workspace_id, ingest_event_id, fingerprint):
            claimed = super().claim_stage_reduction(
                workspace_id, ingest_event_id, fingerprint
            )
            if self.role == "stage":
                try:
                    first_locks.wait(timeout=1)
                except BrokenBarrierError:
                    pass
            return claimed

    def transition():
        service = AccountService(lambda: CoordinatedUnitOfWork("stage"))
        return service.apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                lead_id=lead_id,
                target_stage="meeting_booked",
                identity=IdentityHints(account_id=account_id),
                occurred_at=NOW,
                ingest_event_id=event,
            )
        )

    def append_note():
        return ActivityService(lambda: CoordinatedUnitOfWork("activity")).append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=account_id,
                lead_id=lead_id,
                activity_type="note",
                occurred_at=NOW,
                title="Same-event note",
                ingest_event_id=event,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stage_future = pool.submit(transition)
        note_future = pool.submit(append_note)
        stage_result = stage_future.result(timeout=10)
        note_result = note_future.result(timeout=10)

    with Session(engine) as session:
        activities = session.scalars(
            select(Activity).where(Activity.ingest_event_id == event)
        ).all()
        assert stage_result.lead_id == lead_id
        assert note_result.activity_id in {activity.id for activity in activities}
        assert [activity.activity_type for activity in activities].count(
            "stage_change"
        ) == 1
        assert [activity.activity_type for activity in activities].count("note") == 1


def test_stage_replay_and_activity_correction_do_not_deadlock(engine, workspace):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    with Session(engine) as session, session.begin():
        account = make_account(workspace)
        session.add(account)
        session.flush()
        lead = Lead(workspace_id=workspace, account_id=account.id, stage="contacted")
        session.add(lead)
        session.flush()
        account_id, lead_id = account.id, lead.id

    factory = sessionmaker(engine, expire_on_commit=False)
    command = StageTransitionCommand(
        workspace_id=workspace,
        lead_id=lead_id,
        target_stage="meeting_booked",
        identity=IdentityHints(account_id=account_id),
        occurred_at=NOW,
        ingest_event_id=event,
    )
    first = AccountService(
        lambda: SqlAlchemyUnitOfWork(factory)
    ).apply_stage_transition(command)
    with Session(engine) as session:
        stage_activity = session.scalar(
            select(Activity).where(
                Activity.ingest_event_id == event,
                Activity.activity_type == "stage_change",
            )
        )
        assert stage_activity is not None
        stage_activity_id = stage_activity.id

    first_locks = Barrier(2)

    class CoordinatedUnitOfWork(SqlAlchemyUnitOfWork):
        def __init__(self, role):
            super().__init__(factory)
            self.role = role

        def __enter__(self):
            uow = super().__enter__()
            assert self.session is not None
            self.session.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
            self.session.execute(text("SET LOCAL statement_timeout = '5s'"))
            if self.role == "replay":
                original_replay = self.activities.replay

                def coordinated_replay(workspace_id, ingest_event_id, activity_type):
                    row = original_replay(workspace_id, ingest_event_id, activity_type)
                    try:
                        first_locks.wait(timeout=1)
                    except BrokenBarrierError:
                        pass
                    return row

                self.activities.replay = coordinated_replay
            else:
                original_get = self.leads.get

                def coordinated_get(workspace_id, row_id, *, for_update=False):
                    row = original_get(workspace_id, row_id, for_update=for_update)
                    if for_update and row_id == lead_id:
                        try:
                            first_locks.wait(timeout=1)
                        except BrokenBarrierError:
                            pass
                    return row

                self.leads.get = coordinated_get
            return uow

    def replay_stage():
        return AccountService(
            lambda: CoordinatedUnitOfWork("replay")
        ).apply_stage_transition(command)

    def append_correction():
        return ActivityService(lambda: CoordinatedUnitOfWork("correction")).append(
            AppendActivityCommand(
                workspace_id=workspace,
                account_id=account_id,
                lead_id=lead_id,
                activity_type="note",
                occurred_at=NOW,
                title="Correct stage activity",
                supersedes_activity_id=stage_activity_id,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        replay_future = pool.submit(replay_stage)
        correction_future = pool.submit(append_correction)
        replay_result = replay_future.result(timeout=10)
        correction_result = correction_future.result(timeout=10)

    assert replay_result == first
    with Session(engine) as session:
        activities = session.scalars(
            select(Activity).where(Activity.lead_id == lead_id)
        ).all()
        assert len(activities) == 2
        correction = session.get(Activity, correction_result.activity_id)
        assert correction is not None
        assert correction.supersedes_activity_id == stage_activity_id


def test_activity_service_pg_cross_refs_are_generic_and_leave_no_row(engine, workspace):
    graph = _tenant_graph(engine)
    account_id = graph["account"][0]
    service = ActivityService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    invalid = (
        {"lead_id": graph["lead"][1]},
        {"contact_id": graph["contact"][1]},
        {"source_identity_id": graph["source"][1]},
        {"ingest_event_id": graph["event"][1]},
        {"supersedes_activity_id": graph["activity"][1]},
    )
    with Session(engine) as session:
        before = session.query(Activity).count()
    for mutation in invalid:
        with pytest.raises(ValueError) as exc:
            service.append(
                AppendActivityCommand(
                    workspace_id=graph["w"][0],
                    account_id=account_id,
                    activity_type="note",
                    occurred_at=NOW,
                    title="x",
                    **mutation,
                )
            )
        assert (
            str(exc.value) == "activity requires review"
            and exc.value.__context__ is None
        )
    with Session(engine) as session:
        assert session.query(Activity).count() == before


def test_pg_failure_rolls_back_created_graph_and_source_link(engine, workspace):
    source_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            SourceIdentity(
                id=source_id,
                workspace_id=workspace,
                source_system="manual",
                entity_kind="account",
                source_scope="rollback",
                external_id="rollback",
            )
        )
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    with pytest.raises(IdentityReviewRequired):
        service.apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                target_stage="meeting_booked",
                identity=IdentityHints(
                    company_name="Rollback",
                    domain="rollback.invalid",
                    contact_email="rollback@example.invalid",
                    source_identity_id=source_id,
                ),
                occurred_at=NOW,
                ingest_event_id=uuid4(),
                commercial_classification="confirmed",
            )
        )
    with Session(engine) as session:
        assert (
            session.query(Account).count()
            == session.query(Contact).count()
            == session.query(Lead).count()
            == session.query(Activity).count()
            == 0
        )
        source = session.get(SourceIdentity, source_id)
        assert (
            source.canonical_entity_id is None and source.canonical_entity_type is None
        )
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_pg_replay_different_target_and_lead_entity_conflict(engine, workspace):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    first = service.apply_stage_transition(
        StageTransitionCommand(
            workspace_id=workspace,
            target_stage="meeting_booked",
            identity=IdentityHints(company_name="Replay", domain="replay.invalid"),
            occurred_at=NOW,
            ingest_event_id=event,
            commercial_classification="confirmed",
        )
    )
    with pytest.raises(ReplayConflictError):
        service.apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                target_stage="won",
                identity=IdentityHints(company_name="Replay", domain="replay.invalid"),
                occurred_at=NOW,
                ingest_event_id=event,
                commercial_classification="confirmed",
            )
        )
    with Session(engine) as session, session.begin():
        other = Lead(workspace_id=workspace, account_id=first.account_id)
        session.add(other)
        session.flush()
        other_id = other.id
    with pytest.raises(ReplayConflictError):
        service.apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                lead_id=other_id,
                target_stage="meeting_booked",
                identity=IdentityHints(account_id=first.account_id),
                occurred_at=NOW,
                ingest_event_id=event,
                commercial_classification="confirmed",
            )
        )


def test_pg_existing_lead_replay_changed_explicit_account_conflicts(engine, workspace):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    with Session(engine) as session, session.begin():
        first_account = make_account(
            workspace, display_name="Alpha", normalized_name="alpha"
        )
        second_account = make_account(
            workspace, display_name="Beta", normalized_name="beta"
        )
        session.add_all([first_account, second_account])
        session.flush()
        lead = Lead(workspace_id=workspace, account_id=first_account.id)
        session.add(lead)
        session.flush()
        ids = lead.id, first_account.id, second_account.id
    lead_id, first_account_id, second_account_id = ids
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )

    def transition(account_id):
        return StageTransitionCommand(
            workspace_id=workspace,
            lead_id=lead_id,
            target_stage="meeting_booked",
            identity=IdentityHints(account_id=account_id),
            occurred_at=NOW,
            ingest_event_id=event,
        )

    service.apply_stage_transition(transition(first_account_id))
    with pytest.raises(ReplayConflictError, match="different semantics"):
        service.apply_stage_transition(transition(second_account_id))


def test_pg_source_first_excluded_and_review_create_nothing(engine, workspace):
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    base = dict(
        workspace_id=workspace,
        target_stage="meeting_booked",
        identity=IdentityHints(company_name="No rows", domain="none.invalid"),
        occurred_at=NOW,
    )
    assert (
        service.apply_stage_transition(
            StageTransitionCommand(**base, commercial_classification="excluded")
        ).status
        == "excluded"
    )
    with pytest.raises(IdentityReviewRequired):
        service.apply_stage_transition(
            StageTransitionCommand(**base, commercial_classification="review")
        )
    with Session(engine) as session:
        assert (
            session.query(Account).count()
            == session.query(Lead).count()
            == session.query(Activity).count()
            == 0
        )


def test_pg_stage_reduction_claims_excluded_and_conflicts_with_confirmed(
    engine, workspace
):
    event = uuid4()
    create_ingest_event(engine, workspace, event)
    service = AccountService(
        lambda: SqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    )
    excluded = StageTransitionCommand(
        workspace_id=workspace,
        target_stage="meeting_booked",
        identity=IdentityHints(company_name="Claimed", domain="claimed.invalid"),
        occurred_at=NOW,
        ingest_event_id=event,
        commercial_classification="excluded",
    )
    assert service.apply_stage_transition(excluded).status == "excluded"
    with Session(engine) as session:
        fingerprint = session.get(IngestEvent, event).stage_reduction_fingerprint
        assert fingerprint is not None and len(fingerprint) == 64
        assert session.query(Activity).count() == session.query(Lead).count() == 0
    with pytest.raises(ReplayConflictError, match="different semantics"):
        service.apply_stage_transition(
            replace(excluded, commercial_classification="confirmed")
        )


def test_pg_concurrent_stage_reduction_identical_and_conflicting(engine, workspace):
    factory = sessionmaker(engine, expire_on_commit=False)

    def run(event, stage):
        return AccountService(
            lambda: SqlAlchemyUnitOfWork(factory)
        ).apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                target_stage=stage,
                identity=IdentityHints(
                    company_name="Concurrent", domain="concurrent.invalid"
                ),
                occurred_at=NOW,
                ingest_event_id=event,
            )
        )

    identical_event = uuid4()
    create_ingest_event(engine, workspace, identical_event)
    with ThreadPoolExecutor(max_workers=2) as pool:
        identical = list(
            pool.map(lambda _: run(identical_event, "meeting_booked"), range(2))
        )
    assert identical[0] == identical[1]
    with Session(engine) as session:
        assert session.query(Activity).count() == session.query(Lead).count() == 1

    conflicting_event = uuid4()
    create_ingest_event(engine, workspace, conflicting_event)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, conflicting_event, stage)
            for stage in ("meeting_booked", "proposal_sent")
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ReplayConflictError as exc:
                outcomes.append(exc)
    assert sum(isinstance(item, ReplayConflictError) for item in outcomes) == 1


def test_concurrent_domain_name_with_distinct_sources_converges_and_both_succeed(
    engine, workspace
):
    sources, events = (uuid4(), uuid4()), (uuid4(), uuid4())
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                SourceIdentity(
                    id=s,
                    workspace_id=workspace,
                    source_system="manual",
                    entity_kind="account",
                    source_scope="concurrent",
                    external_id=s.hex,
                )
                for s in sources
            ]
        )
    for event in events:
        create_ingest_event(engine, workspace, event)
    factory = sessionmaker(engine, expire_on_commit=False)

    def apply(pair):
        source, event = pair
        return AccountService(
            lambda: SqlAlchemyUnitOfWork(factory)
        ).apply_stage_transition(
            StageTransitionCommand(
                workspace_id=workspace,
                target_stage="meeting_booked",
                identity=IdentityHints(
                    company_name="Shared Company",
                    domain="shared.invalid",
                    source_identity_id=source,
                ),
                occurred_at=NOW,
                ingest_event_id=event,
                commercial_classification="confirmed",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, zip(sources, events, strict=True)))
    assert len({result.account_id for result in results}) == 1
    with Session(engine) as session:
        assert (
            session.query(Account).count() == 1
            and session.query(Lead).count() == 2
            and session.query(Activity).count() == 2
        )
        assert {
            row.canonical_entity_id
            for row in session.scalars(select(SourceIdentity)).all()
        } == {results[0].account_id}


def test_migration_lifecycle_from_foreign_cwd_restores_head(engine, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    command = [sys.executable, "-m", "alembic", "-c", str(CONFIG)]

    def run(*args):
        result = subprocess.run(
            command + list(args), cwd=tmp_path, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        assert database_url() not in result.stdout + result.stderr
        return result

    try:
        assert run("current").stdout.strip() == run("heads").stdout.strip()
        run("downgrade", "0006")
        with engine.connect() as connection:
            entity_kind_constraint = connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_source_identities_entity_kind'"
                )
            )
        assert entity_kind_constraint is not None
        assert "mailbox" not in entity_kind_constraint
        run("upgrade", "head")
        run("downgrade", "0001")
        inspector = inspect(engine)
        assert {
            "workspaces",
            "source_identities",
            "ingest_events",
            "sync_checkpoints",
        } <= set(inspector.get_table_names())
        assert not {"accounts", "contacts", "leads", "activities"}.intersection(
            inspector.get_table_names()
        )
        assert not {
            "proposals",
            "proposal_versions",
            "proposal_items",
            "proposal_followups",
        }.intersection(inspector.get_table_names())
        assert "uq_source_identities_workspace_id" not in {
            row["name"] for row in inspector.get_unique_constraints("source_identities")
        }
        assert "uq_ingest_events_workspace_id" not in {
            row["name"] for row in inspector.get_unique_constraints("ingest_events")
        }
        assert "stage_reduction_fingerprint" not in {
            row["name"] for row in inspector.get_columns("ingest_events")
        }
        run("upgrade", "head")
        assert "No new upgrade operations detected" in run("check").stdout
        inspector = inspect(engine)
        assert {"accounts", "contacts", "leads", "activities"} <= set(
            inspector.get_table_names()
        )
        assert {
            "proposals",
            "proposal_versions",
            "proposal_items",
            "proposal_followups",
            "evidence",
            "review_candidates",
            "recommendations",
            "outbox_events",
            "audit_events",
        } <= set(inspector.get_table_names())
        assert "uq_source_identities_workspace_id" in {
            row["name"] for row in inspector.get_unique_constraints("source_identities")
        }
        assert "uq_ingest_events_workspace_id" in {
            row["name"] for row in inspector.get_unique_constraints("ingest_events")
        }
        assert "stage_reduction_fingerprint" in {
            row["name"] for row in inspector.get_columns("ingest_events")
        }
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgname = 'trg_crm_activities_validate_context' "
                        "AND NOT tgisinternal"
                    )
                ).scalar_one()
                == 1
            )
        workspace_id = uuid4()
        account_id = uuid4()
        activity_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, slug, name) VALUES (:id, :slug, 'Lifecycle')"
                ),
                {"id": workspace_id, "slug": f"lifecycle-{workspace_id.hex}"},
            )
            connection.execute(
                text(
                    "INSERT INTO accounts (id, workspace_id, display_name, normalized_name) VALUES (:id, :workspace, 'Lifecycle', 'lifecycle')"
                ),
                {"id": account_id, "workspace": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO activities (id, workspace_id, account_id, activity_type, occurred_at, title) VALUES (:id, :workspace, :account, 'note', :occurred, 'Original')"
                ),
                {
                    "id": activity_id,
                    "workspace": workspace_id,
                    "account": account_id,
                    "occurred": NOW,
                },
            )
        with (
            engine.begin() as connection,
            pytest.raises(ProgrammingError, match="immutable"),
        ):
            connection.execute(
                text("UPDATE activities SET title='Changed' WHERE id=:id"),
                {"id": activity_id},
            )
    finally:
        run("upgrade", "head")
