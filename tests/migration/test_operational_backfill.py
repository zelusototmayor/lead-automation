from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.crm.migration.backfill import _scope, backfill_accounts
from src.crm.migration.operational_backfill import (
    artifact_uuid,
    backfill_legacy_operations,
)
from src.crm.migration.proposals_backfill import backfill_proposals
from src.crm.migration.sheets_snapshot import snapshot_sheet
from src.crm.persistence.models import (
    Activity,
    IngestEvent,
    Lead,
    Proposal,
    Task,
    Workspace,
)

from ._postgres import cleanup_workspace, require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]


class OperationsFixtureSource:
    def read_values(self, spreadsheet_id, sheet_name):
        return json.loads(
            (ROOT / "tests/fixtures/legacy_operations_rows.json").read_text()
        )


class CanonicalOperationsFixtureSource:
    def read_values(self, spreadsheet_id, sheet_name):
        return json.loads(
            (ROOT / "tests/fixtures/canonical_legacy_operations_rows.json").read_text()
        )


def _snapshot(values):
    class Source:
        def read_values(self, spreadsheet_id, sheet_name):
            return values

    return snapshot_sheet(
        Source(), "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
    )


def operations_snapshot():
    return snapshot_sheet(
        OperationsFixtureSource(),
        "fixture-spreadsheet",
        "PT Logistics",
        stable_id_column="ID",
    )


def canonical_operations_snapshot():
    return snapshot_sheet(
        CanonicalOperationsFixtureSource(),
        "fixture-spreadsheet",
        "PT Logistics",
        stable_id_column="ID",
    )


def canonical_identity_snapshot():
    fixture = json.loads(
        (ROOT / "tests/fixtures/canonical_legacy_operations_rows.json").read_text()
    )
    headers = fixture[0]
    id_index = headers.index("ID")
    company_index = headers.index("Company")
    values = [["ID", "Company", "Stage"]] + [
        [row[id_index], row[company_index], "Meeting Booked"] for row in fixture[1:]
    ]

    class Source:
        def read_values(self, spreadsheet_id, sheet_name):
            return values

    return snapshot_sheet(
        Source(),
        "fixture-spreadsheet",
        "PT Logistics",
        stable_id_column="ID",
    )


def test_dry_run_classifies_real_legacy_headers_without_writes():
    report = backfill_legacy_operations(operations_snapshot())

    assert report.safe_dict() == {
        "input_rows": 3,
        "task_candidates": 4,
        "note_candidates": 4,
        "tasks_created": 0,
        "activities_created": 0,
        "replay_noop": 0,
        "conflicts": 2,
        "review_reasons": {
            "invalid_next_call_date": 1,
            "unknown_outreach": 1,
        },
        "full_history_unavailable": True,
        "applied": False,
    }


def test_dry_run_reports_missing_call_time_and_ambiguous_outreach_columns():
    values = [
        [
            "ID",
            "Next Call Date",
            "Next Call Time",
            "Follow-Up Due",
            "Outreach",
            "Outreach Method",
        ],
        ["missing-time", "2026-07-22", "", "", "", ""],
        ["ambiguous", "", "", "2026-07-23", "email", "call"],
    ]

    class Source:
        def read_values(self, spreadsheet_id, sheet_name):
            return values

    snapshot = snapshot_sheet(
        Source(), "fixture-spreadsheet", "PT Logistics", stable_id_column="ID"
    )
    report = backfill_legacy_operations(snapshot)

    assert report.task_candidates == 0
    assert report.conflicts == 2
    assert report.review_reasons == {
        "missing_next_call_time": 1,
        "ambiguous_outreach": 1,
    }


def test_dry_run_classifies_preserved_production_header_vocabulary():
    report = backfill_legacy_operations(canonical_operations_snapshot())

    assert report.safe_dict() == {
        "input_rows": 6,
        "task_candidates": 4,
        "note_candidates": 3,
        "tasks_created": 0,
        "activities_created": 0,
        "replay_noop": 0,
        "conflicts": 3,
        "review_reasons": {
            "terminal_stage_due": 1,
            "ambiguous_outreach_sequence": 1,
            "unknown_last_touch_type": 1,
        },
        "full_history_unavailable": True,
        "applied": False,
    }


@pytest.mark.parametrize(
    ("headers", "row", "reason"),
    [
        (
            ["ID", "Stage", "Due"],
            ["won-due", "Won", "2026-07-22"],
            "terminal_stage_due",
        ),
        (
            ["ID", "Stage", "Next Call Date", "Next Call Time"],
            ["lost-call", "Lost", "2026-07-22", "09:00"],
            "terminal_stage_next_call",
        ),
        (
            ["ID", "Stage", "Proposal Next Action Due"],
            ["lost-proposal", "Lost", "2026-07-22"],
            "terminal_stage_proposal_next_action_due",
        ),
    ],
)
def test_terminal_rows_never_create_open_task_candidates(headers, row, reason):
    report = backfill_legacy_operations(_snapshot([headers, row]))

    assert report.task_candidates == 0
    assert report.review_reasons == {reason: 1}


@pytest.mark.parametrize(
    ("day", "clock"),
    [("2026-03-29", "01:30"), ("2026-10-25", "01:30")],
)
def test_dst_nonexistent_or_ambiguous_due_time_requires_review(day, clock):
    report = backfill_legacy_operations(
        _snapshot(
            [
                ["ID", "Stage", "Due", "Due Time"],
                ["dst-row", "Call Back", day, clock],
            ]
        )
    )

    assert report.task_candidates == 0
    assert report.review_reasons == {"invalid_or_ambiguous_local_time": 1}


def test_cli_is_dry_run_by_default(tmp_path):
    from src.crm.migration.sheets_snapshot import save_snapshot

    snapshot_path = tmp_path / "operations.json"
    save_snapshot(operations_snapshot(), snapshot_path)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/crm_backfill_legacy_operations.py",
            "--snapshot",
            str(snapshot_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["applied"] is False
    assert json.loads(result.stdout)["full_history_unavailable"] is True


def test_apply_rejects_missing_explicit_postgres_identity():
    snapshot = operations_snapshot()

    for kwargs in (
        {"apply": True},
        {
            "apply": True,
            "database_url": "sqlite://",
            "workspace_id": uuid4(),
            "owner_user_id": uuid4(),
        },
        {
            "apply": True,
            "database_url": "postgresql+psycopg://localhost/test",
            "workspace_id": "not-a-uuid",
            "owner_user_id": uuid4(),
        },
        {
            "apply": True,
            "database_url": "postgresql+psycopg://localhost/test",
            "workspace_id": uuid4(),
            "owner_user_id": None,
        },
    ):
        try:
            backfill_legacy_operations(snapshot, **kwargs)
        except ValueError as exc:
            assert "explicit" in str(exc)
        else:
            raise AssertionError("unsafe apply configuration was accepted")


def test_deterministic_ids_are_stable_and_workspace_scoped():
    workspace = UUID("00000000-0000-0000-0000-000000000001")
    other_workspace = UUID("00000000-0000-0000-0000-000000000002")

    first = artifact_uuid(workspace, "scope", "lead-1", "task", "next_call")
    assert first == artifact_uuid(workspace, "scope", "lead-1", "task", "next_call")
    assert first != artifact_uuid(
        other_workspace, "scope", "lead-1", "task", "next_call"
    )


def test_postgres_apply_replay_and_changed_current_task_goes_to_review():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    owner_id = uuid4()
    engine = create_engine(database_url)
    snapshot = operations_snapshot()
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"operations-{workspace_id}",
                    name="Operations Fixture",
                )
            )
        account_report = backfill_accounts(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        first = backfill_legacy_operations(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            owner_user_id=owner_id,
        )
        replay = backfill_legacy_operations(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            owner_user_id=owner_id,
        )

        assert account_report.imported == 3
        assert first.tasks_created == 3
        assert first.activities_created == 4
        assert first.review_reasons == {
            "invalid_next_call_date": 1,
            "unknown_outreach": 1,
            "missing_proposal_identity": 1,
        }
        assert replay.tasks_created == 0
        assert replay.activities_created == 0
        assert replay.replay_noop == 7
        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.workspace_id == workspace_id)
                )
                == 3
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Activity)
                    .where(
                        Activity.workspace_id == workspace_id,
                        Activity.source_system == "google_sheets",
                        Activity.activity_type != "stage_change",
                    )
                )
                == 4
            )
            pre_account_lead = session.scalar(
                select(Lead)
                .join(
                    Task,
                    (Task.workspace_id == Lead.workspace_id)
                    & (Task.lead_id == Lead.id),
                )
                .where(
                    Task.workspace_id == workspace_id,
                    Task.source_rule == "legacy_sheet:next_call",
                )
            )
            assert pre_account_lead.account_id is None
            assert all(
                activity.occurred_at == activity.recorded_at
                or "timestamp unavailable" in activity.title
                for activity in session.scalars(
                    select(Activity).where(
                        Activity.workspace_id == workspace_id,
                        Activity.activity_type != "stage_change",
                    )
                )
            )

        scope = _scope(snapshot)
        changed_id = artifact_uuid(
            workspace_id, scope, "lead-pre-account", "task", "next_call"
        )
        with Session(engine) as session, session.begin():
            session.get(Task, changed_id).title = "Operator-edited task"

        conflict = backfill_legacy_operations(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            owner_user_id=owner_id,
        )
        assert conflict.tasks_created == 0
        assert conflict.conflicts == 4
        assert conflict.review_reasons == {
            "invalid_next_call_date": 1,
            "unknown_outreach": 1,
            "missing_proposal_identity": 1,
            "conflicting_current_task": 1,
        }
        assert conflict.replay_noop == 6
        with Session(engine) as session:
            assert session.get(Task, changed_id).title == "Operator-edited task"
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_canonical_apply_types_due_work_and_links_proposal_follow_up():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    owner_id = uuid4()
    engine = create_engine(database_url)
    snapshot = canonical_operations_snapshot()
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"canonical-operations-{workspace_id}",
                    name="Canonical Operations Fixture",
                )
            )
        backfill_accounts(
            canonical_identity_snapshot(),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        backfill_proposals(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        report = backfill_legacy_operations(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            owner_user_id=owner_id,
        )

        assert report.tasks_created == 4
        assert report.activities_created == 3
        with Session(engine) as session:
            tasks = list(
                session.scalars(select(Task).where(Task.workspace_id == workspace_id))
            )
            assert any(
                task.source_rule == "legacy_sheet:due"
                and task.task_type in {"email", "call"}
                for task in tasks
            )
            assert {task.task_type for task in tasks} == {
                "email",
                "call",
                "follow_up",
            }
            proposal_task = next(
                task
                for task in tasks
                if task.source_rule == "legacy_sheet:proposal_next_action_due"
            )
            proposal = session.scalar(
                select(Proposal).where(Proposal.workspace_id == workspace_id)
            )
            assert proposal_task.proposal_id == proposal.id
            assert any(
                task.source_rule == "legacy_sheet:outreach_fu2_due"
                and task.task_type == "email"
                for task in tasks
            )
            activities = list(
                session.scalars(
                    select(Activity).where(
                        Activity.workspace_id == workspace_id,
                        Activity.activity_type != "stage_change",
                    )
                )
            )
            assert {activity.activity_type for activity in activities} == {
                "note",
                "email_sent",
                "call",
            }
            assert all(
                "time unavailable" in activity.title
                for activity in activities
                if activity.activity_type != "note"
            )
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_proposal_follow_up_without_exact_proposal_requires_review():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    owner_id = uuid4()
    engine = create_engine(database_url)
    snapshot = _snapshot(
        [
            ["ID", "Company", "Stage", "Proposal Next Action Due"],
            ["proposal-missing", "Example", "Proposal Sent", "2026-07-22"],
        ]
    )
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"missing-proposal-{workspace_id}",
                    name="Missing Proposal Fixture",
                )
            )
        backfill_accounts(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        report = backfill_legacy_operations(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
            owner_user_id=owner_id,
        )

        assert report.tasks_created == 0
        assert report.review_reasons == {"missing_proposal_identity": 1}
        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.workspace_id == workspace_id)
                )
                == 0
            )
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_unexpected_failure_rolls_back_the_entire_operational_batch():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    owner_id = uuid4()
    engine = create_engine(database_url)
    snapshot = operations_snapshot()
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"operations-rollback-{workspace_id}",
                    name="Operations Rollback Fixture",
                )
            )
        backfill_accounts(
            snapshot,
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        def fail_after_first(phase: str, artifact_kind: str, index: int) -> None:
            if (phase, artifact_kind, index) == ("after", "task", 0):
                raise RuntimeError("injected operational failure")

        with pytest.raises(RuntimeError, match="injected operational failure"):
            backfill_legacy_operations(
                snapshot,
                apply=True,
                database_url=database_url,
                workspace_id=workspace_id,
                owner_user_id=owner_id,
                failure_injector=fail_after_first,
            )

        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.workspace_id == workspace_id)
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Activity)
                    .where(
                        Activity.workspace_id == workspace_id,
                        Activity.activity_type != "stage_change",
                    )
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(IngestEvent)
                    .where(
                        IngestEvent.workspace_id == workspace_id,
                        IngestEvent.event_type.like("sheets.legacy_%_backfill"),
                    )
                )
                == 0
            )
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()
