from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.crm.migration.backfill import _scope, _snapshot_hash, backfill_accounts
from src.crm.migration.sheets_snapshot import SheetSnapshot, snapshot_sheet
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    IngestEvent,
    Lead,
    SourceIdentity,
    SyncCheckpoint,
    Workspace,
)

from ._postgres import cleanup_workspace, require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]


class FixtureSource:
    def __init__(self):
        self.values = json.loads(
            (ROOT / "tests/fixtures/pt_logistics_rows.json").read_text()
        )

    def read_values(self, spreadsheet_id, sheet_name):
        return self.values


def fixture_snapshot():
    return snapshot_sheet(
        FixtureSource(), "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
    )


def test_dry_run_reports_duplicates_unmapped_and_homonyms_without_writes():
    report = backfill_accounts(fixture_snapshot(), apply=False)

    assert report.input_rows == 7
    assert report.snapshot_rows == 5
    assert report.duplicates == 1
    assert report.unmapped_stages == {"unmapped": 1}
    assert report.imported == 4
    assert report.accounts_created_or_linked == 3
    assert report.conflicts == 1
    assert report.review_reasons == {"duplicate_stable_id": 1}
    assert report.replay_noop == 0
    assert report.applied is False


def test_terminal_stage_without_history_is_review_only_in_dry_run():
    source = FixtureSource()
    source.values = [
        source.values[0],
        ["terminal", "Company", "", "", "", "Logistics", "lost", "fixture"],
    ]

    report = backfill_accounts(
        snapshot_sheet(
            source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
        ),
        apply=False,
    )

    assert report.imported == 0
    assert report.conflicts == 1
    assert report.review_reasons == {"history_required": 1}


def test_sheet_scope_encoding_cannot_collide_on_path_separators():
    left = snapshot_sheet(FixtureSource(), "a/b", "c", stable_id_column="ID")
    right = snapshot_sheet(FixtureSource(), "a", "b/c", stable_id_column="ID")

    assert _scope(left) != _scope(right)


def test_postgres_apply_and_identical_replay_create_no_new_rows():
    database_url = require_disposable_postgres()
    migration = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "migrations/alembic.ini",
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert migration.returncode == 0, migration.stderr
    engine = create_engine(database_url)
    workspace_id = uuid4()
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"migration-fixture-{workspace_id}",
                    name="Migration Fixture",
                )
            )

        first = backfill_accounts(
            fixture_snapshot(),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        with Session(engine) as session:
            counts_after_first = tuple(
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
                for model in (Account, Contact, Lead, Activity)
            )
            accountful_leads = session.scalars(
                select(Lead).where(
                    Lead.workspace_id == workspace_id,
                    Lead.account_id.is_not(None),
                )
            ).all()
            assert accountful_leads
            assert all(
                (
                    lead.company_name,
                    lead.contact_name,
                    lead.contact_email,
                    lead.contact_phone,
                    lead.city,
                )
                == (None, None, None, None, None)
                for lead in accountful_leads
            )
        moved_source = FixtureSource()
        moved_source.values[1], moved_source.values[2] = (
            moved_source.values[2],
            moved_source.values[1],
        )
        replay = backfill_accounts(
            snapshot_sheet(
                moved_source,
                "fixture-spreadsheet",
                "PT Logistics",
                stable_id_column="ID",
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        with Session(engine) as session:
            counts_after_replay = tuple(
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
                for model in (Account, Contact, Lead, Activity)
            )

        assert first.imported == 4
        assert first.accounts_created_or_linked == 3
        assert counts_after_first == (3, 3, 4, 4)
        with Session(engine) as session:
            checkpoint = session.scalar(
                select(SyncCheckpoint).where(
                    SyncCheckpoint.workspace_id == workspace_id
                )
            )
            assert checkpoint is not None
            first_cursor = checkpoint.cursor_encrypted
        assert replay.imported == 0
        assert replay.replay_noop == 4
        assert counts_after_replay == counts_after_first
        with Session(engine) as session:
            replay_checkpoint = session.scalar(
                select(SyncCheckpoint).where(
                    SyncCheckpoint.workspace_id == workspace_id
                )
            )
            assert replay_checkpoint.cursor_encrypted == first_cursor

        conflict_source = FixtureSource()
        conflict_source.values = [
            conflict_source.values[0],
            [
                "safe-partial",
                "Safe Partial",
                "",
                "",
                "",
                "Logistics",
                "Contacted",
                "Sheet fixture",
            ],
            [
                "ambiguous-exact",
                "Same Name",
                "Conflict Example",
                "one@same-one.example",
                "https://same-two.example",
                "Logistics",
                "Meeting Booked",
                "Sheet fixture",
            ],
        ]
        conflict = backfill_accounts(
            snapshot_sheet(
                conflict_source,
                "fixture-spreadsheet",
                "PT Logistics",
                stable_id_column="ID",
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        assert conflict.imported == 1
        assert conflict.conflicts == 1
        assert conflict.review_reasons == {"identity_conflict": 1}

        terminal_source = FixtureSource()
        terminal_source.values = [
            terminal_source.values[0],
            ["terminal", "Company", "", "", "", "Logistics", "lost", "fixture"],
        ]
        terminal = backfill_accounts(
            snapshot_sheet(
                terminal_source,
                "fixture-spreadsheet",
                "PT Logistics",
                stable_id_column="ID",
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        assert terminal.imported == 0
        assert terminal.conflicts == 1
        assert terminal.review_reasons == {"history_required": 1}
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_backfill_rejects_unreviewed_exit_from_terminal_stage():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    source = FixtureSource()
    source.values = [
        source.values[0],
        ["terminal-change", "Company", "", "", "", "Logistics", "Won", "fixture"],
    ]
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"terminal-change-{workspace_id}",
                    name="Terminal Change Fixture",
                )
            )
        first = backfill_accounts(
            snapshot_sheet(
                source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        source.values[1][6] = "Contacted"

        changed = backfill_accounts(
            snapshot_sheet(
                source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        with Session(engine) as session:
            lead = session.scalar(select(Lead).where(Lead.workspace_id == workspace_id))
            activity_count = session.scalar(
                select(func.count())
                .select_from(Activity)
                .where(Activity.workspace_id == workspace_id)
            )
        assert first.imported == 1
        assert changed.imported == 0
        assert changed.conflicts == 1
        assert changed.review_reasons == {"invalid_transition": 1}
        assert lead.stage == "won"
        assert activity_count == 1
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_existing_account_lifecycle_advances_with_higher_stage():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    source = FixtureSource()
    source.values = [source.values[0], source.values[1]]
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"lifecycle-{workspace_id}",
                    name="Lifecycle Fixture",
                )
            )
        backfill_accounts(
            snapshot_sheet(
                source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        source.values[1][6] = "Won"
        backfill_accounts(
            snapshot_sheet(
                source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        with Session(engine) as session:
            account = session.scalar(
                select(Account).where(Account.workspace_id == workspace_id)
            )
        assert account.lifecycle_stage == "customer"
        assert account.highest_stage_rank == 90
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_backfill_persists_canonical_account_city():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    source = FixtureSource()
    source.values = [
        [*source.values[0], "City", "Phone"],
        [*source.values[1], "Lisboa", "+351000000123"],
    ]
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"city-backfill-{workspace_id}",
                    name="City Backfill Fixture",
                )
            )
        report = backfill_accounts(
            snapshot_sheet(
                source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        with Session(engine) as session:
            account = session.scalar(
                select(Account).where(Account.workspace_id == workspace_id)
            )
            contact = session.scalar(
                select(Contact).where(Contact.workspace_id == workspace_id)
            )
        assert report.imported == 1
        assert account is not None
        assert account.city == "Lisboa"
        assert contact is not None
        assert contact.phone == "+351000000123"
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_backfill_preserves_pre_account_lead_identity_without_creating_account():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    source = FixtureSource()
    source.values = [
        [*source.values[0], "City", "Phone"],
        [
            "pre-account",
            "Early Prospect",
            "Carla Contact",
            "carla@early.example",
            "https://early.example",
            "Technology",
            "Contacted",
            "Sheet fixture",
            "Porto",
            "+351220000000",
        ],
    ]
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"pre-account-{workspace_id}",
                    name="Pre-account Backfill Fixture",
                )
            )

        report = backfill_accounts(
            snapshot_sheet(
                source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
            ),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        with Session(engine) as session:
            lead = session.scalar(select(Lead).where(Lead.workspace_id == workspace_id))
            account_count = session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.workspace_id == workspace_id)
            )
        assert report.imported == 1
        assert account_count == 0
        assert lead.account_id is None
        assert lead.company_name == "Early Prospect"
        assert lead.contact_name == "Carla Contact"
        assert str(lead.contact_email) == "carla@early.example"
        assert lead.contact_phone == "+351220000000"
        assert lead.city == "Porto"
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_concurrent_exact_account_evidence_creates_one_account():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    header = FixtureSource().values[0]

    def candidate(external_id: str):
        source = FixtureSource()
        source.values = [
            header,
            [
                external_id,
                "Same Exact Company",
                "",
                "",
                "https://same-exact.example",
                "Logistics",
                "Meeting Booked",
                "fixture",
            ],
        ]
        return snapshot_sheet(
            source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
        )

    barrier = Barrier(2)

    def apply(snapshot):
        def synchronize(phase: str, index: int):
            if phase == "before" and index == 0:
                barrier.wait(timeout=10)

        return backfill_accounts(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            failure_injector=synchronize,
        )

    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"concurrent-{workspace_id}",
                    name="Concurrent Fixture",
                )
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(apply, [candidate("first"), candidate("second")]))

        with Session(engine) as session:
            account_count = session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.workspace_id == workspace_id)
            )
        assert [report.imported for report in reports] == [1, 1]
        assert account_count == 1
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


@pytest.mark.parametrize(("phase", "index"), [("before", 0), ("after", 1)])
def test_unexpected_failure_rolls_back_entire_batch_and_checkpoint(phase, index):
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"rollback-{workspace_id}",
                    name="Rollback Fixture",
                )
            )

        def inject(candidate_phase: str, candidate_index: int) -> None:
            if (candidate_phase, candidate_index) == (phase, index):
                raise RuntimeError("injected database boundary failure")

        with pytest.raises(RuntimeError, match="injected database boundary failure"):
            backfill_accounts(
                fixture_snapshot(),
                apply=True,
                database_url=database_url,
                workspace_id=workspace_id,
                failure_injector=inject,
            )

        with Session(engine) as session:
            for model in (
                Account,
                Contact,
                Lead,
                Activity,
                SourceIdentity,
                IngestEvent,
                SyncCheckpoint,
            ):
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.workspace_id == workspace_id)
                    )
                    == 0
                )
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_concurrent_identical_snapshot_is_one_import_and_one_replay():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    snapshot = fixture_snapshot()
    barrier = Barrier(2)

    def apply(_):
        def synchronize(phase: str, index: int):
            if phase == "before" and index == 0:
                barrier.wait(timeout=10)

        return backfill_accounts(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            failure_injector=synchronize,
        )

    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"identical-{workspace_id}",
                    name="Identical Replay Fixture",
                )
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(apply, range(2)))

        assert sorted(report.imported for report in reports) == [0, 4]
        assert sorted(report.replay_noop for report in reports) == [0, 4]
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_snapshot_hash_includes_duplicate_ids_and_missing_row_locators():
    base = fixture_snapshot()
    different_duplicate = SheetSnapshot(
        base.spreadsheet_id,
        base.sheet_name,
        base.stable_id_column,
        base.input_rows,
        base.rows,
        ("different-duplicate",),
        base.missing_id_rows,
    )
    missing_at_two = SheetSnapshot(
        base.spreadsheet_id,
        base.sheet_name,
        base.stable_id_column,
        3,
        (),
        (),
        (2,),
    )
    missing_at_three = SheetSnapshot(
        base.spreadsheet_id,
        base.sheet_name,
        base.stable_id_column,
        3,
        (),
        (),
        (3,),
    )

    assert _snapshot_hash(base) != _snapshot_hash(different_duplicate)
    assert _snapshot_hash(missing_at_two) != _snapshot_hash(missing_at_three)


def test_concurrent_distinct_events_claim_one_source_identity():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    base_source = FixtureSource()
    header = list(base_source.values[0])
    base_row = list(base_source.values[1])

    def candidate(origin: str):
        source = FixtureSource()
        row = list(base_row)
        row[7] = origin
        source.values = [header, row]
        return snapshot_sheet(
            source, "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
        )

    barrier = Barrier(2)

    def apply(snapshot):
        def synchronize(phase: str, index: int):
            if phase == "before" and index == 0:
                barrier.wait(timeout=10)

        return backfill_accounts(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            failure_injector=synchronize,
        )

    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"identity-race-{workspace_id}",
                    name="Identity Race Fixture",
                )
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(apply, [candidate("one"), candidate("two")]))

        with Session(engine) as session:
            identity_count = session.scalar(
                select(func.count())
                .select_from(SourceIdentity)
                .where(
                    SourceIdentity.workspace_id == workspace_id,
                    SourceIdentity.entity_kind == "lead",
                )
            )
        assert [report.imported for report in reports] == [1, 1]
        assert identity_count == 1
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()
