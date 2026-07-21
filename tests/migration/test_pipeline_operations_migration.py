from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"


def _run_alembic(command: str, revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(CONFIG),
            command,
            revision,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )


def _alembic(command: str, revision: str) -> None:
    result = _run_alembic(command, revision)
    assert result.returncode == 0, result.stderr


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        if not inspect(engine).has_table("alembic_version"):
            return None
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
    finally:
        engine.dispose()


def _reset_database(database_url: str) -> None:
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _fresh_migration_lifecycle():
    source_url = require_disposable_postgres()
    parsed = make_url(source_url)
    database_name = f"crm_pipeline_migration_test_{uuid4().hex}"
    database_url = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    maintenance_url = parsed.set(database="postgres").render_as_string(
        hide_password=False
    )
    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    previous_database_url = os.environ["DATABASE_URL"]
    head_revision: str | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        os.environ["DATABASE_URL"] = database_url
        assert _current_revision(database_url) is None
        _alembic("upgrade", "head")
        head_revision = _current_revision(database_url)
        assert head_revision is not None
        yield
    finally:
        try:
            if os.environ.get("DATABASE_URL") == database_url:
                _reset_database(database_url)
                _alembic("upgrade", "head")
                restored_revision = _current_revision(database_url)
                assert restored_revision is not None
                if head_revision is not None:
                    assert restored_revision == head_revision
                engine = create_engine(database_url)
                try:
                    with engine.connect() as connection:
                        assert (
                            connection.scalar(text("SELECT count(*) FROM workspaces"))
                            == 0
                        )
                        assert (
                            connection.scalar(text("SELECT count(*) FROM tasks")) == 0
                        )
                finally:
                    engine.dispose()
        finally:
            os.environ["DATABASE_URL"] = previous_database_url
            with maintenance_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
            maintenance_engine.dispose()


def _insert_0007_task_fixture(*, lead_count: int) -> tuple[str, list[str], list[str]]:
    database_url = require_disposable_postgres()
    workspace_id = str(uuid4())
    account_id = str(uuid4())
    lead_ids = [str(uuid4()) for _ in range(lead_count)]
    task_ids = [str(uuid4()), str(uuid4())]
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, slug, name) "
                    "VALUES (:id, :slug, 'Pipeline migration fixture')"
                ),
                {"id": workspace_id, "slug": f"pipeline-migration-{workspace_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, workspace_id, display_name, normalized_name) "
                    "VALUES (:id, :workspace_id, 'Fixture account', :normalized_name)"
                ),
                {
                    "id": account_id,
                    "workspace_id": workspace_id,
                    "normalized_name": f"fixture-{account_id}",
                },
            )
            for lead_id in lead_ids:
                connection.execute(
                    text(
                        "INSERT INTO leads (id, workspace_id, account_id) "
                        "VALUES (:id, :workspace_id, :account_id)"
                    ),
                    {
                        "id": lead_id,
                        "workspace_id": workspace_id,
                        "account_id": account_id,
                    },
                )
            for task_id, task_type in zip(task_ids, ("call", "email"), strict=True):
                connection.execute(
                    text(
                        "INSERT INTO tasks "
                        "(id, workspace_id, account_id, task_type, title, due_at, owner_user_id) "
                        "VALUES (:id, :workspace_id, :account_id, :task_type, "
                        "'Legacy operational task', now(), :owner_user_id)"
                    ),
                    {
                        "id": task_id,
                        "workspace_id": workspace_id,
                        "account_id": account_id,
                        "task_type": task_type,
                        "owner_user_id": str(uuid4()),
                    },
                )
    finally:
        engine.dispose()
    return workspace_id, lead_ids, task_ids


def _delete_task_fixture(database_url: str, workspace_id: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            for table in ("tasks", "leads", "accounts", "workspaces"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id")
                    if table != "workspaces"
                    else text("DELETE FROM workspaces WHERE id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )
    finally:
        engine.dispose()


def test_0008_adds_pipeline_operation_columns() -> None:
    database_url = require_disposable_postgres()
    try:
        _alembic("downgrade", "0007")
        _alembic("upgrade", "0008")

        engine = create_engine(database_url)
        try:
            task_columns = {
                column["name"] for column in inspect(engine).get_columns("tasks")
            }
            activity_columns = {
                column["name"] for column in inspect(engine).get_columns("activities")
            }
            assert "lead_id" in task_columns
            assert "outcome_code" in activity_columns
            task_foreign_keys = {
                constraint["name"]
                for constraint in inspect(engine).get_foreign_keys("tasks")
            }
            task_indexes = {
                index["name"] for index in inspect(engine).get_indexes("tasks")
            }
            activity_checks = {
                constraint["name"]
                for constraint in inspect(engine).get_check_constraints("activities")
            }
            assert "fk_tasks_workspace_account_lead" in task_foreign_keys
            assert "ix_tasks_workspace_lead_status_due" in task_indexes
            assert "ck_activities_outcome_code_nonblank" in activity_checks
        finally:
            engine.dispose()
    finally:
        _alembic("upgrade", "head")


def test_0007_operational_tasks_are_backfilled_and_survive_full_cycle() -> None:
    database_url = require_disposable_postgres()
    workspace_id: str | None = None
    try:
        _alembic("downgrade", "0007")
        workspace_id, lead_ids, task_ids = _insert_0007_task_fixture(lead_count=1)

        for _ in range(2):
            _alembic("upgrade", "head")
            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    assigned_leads = (
                        connection.execute(
                            text(
                                "SELECT lead_id::text FROM tasks "
                                "WHERE id::text = ANY(:task_ids) ORDER BY id"
                            ),
                            {"task_ids": task_ids},
                        )
                        .scalars()
                        .all()
                    )
                assert assigned_leads == [lead_ids[0], lead_ids[0]]
            finally:
                engine.dispose()
            _alembic("downgrade", "0007")
    finally:
        try:
            if workspace_id is not None:
                _delete_task_fixture(database_url, workspace_id)
        finally:
            _alembic("upgrade", "head")


@pytest.mark.parametrize("lead_count", [0, 2], ids=["missing", "ambiguous"])
def test_0008_aborts_without_changes_when_lead_assignment_is_not_unique(
    lead_count: int,
) -> None:
    database_url = require_disposable_postgres()
    workspace_id: str | None = None
    try:
        _alembic("downgrade", "0007")
        workspace_id, _, task_ids = _insert_0007_task_fixture(lead_count=lead_count)
        result = _run_alembic("upgrade", "0008")

        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert "cannot assign lead context to existing operational tasks" in output
        assert all(task_id not in output for task_id in task_ids)

        engine = create_engine(database_url)
        try:
            assert "lead_id" not in {
                column["name"] for column in inspect(engine).get_columns("tasks")
            }
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    == "0007"
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM tasks WHERE id::text = ANY(:task_ids)"
                        ),
                        {"task_ids": task_ids},
                    ).scalar_one()
                    == 2
                )
        finally:
            engine.dispose()
    finally:
        try:
            if workspace_id is not None:
                _delete_task_fixture(database_url, workspace_id)
        finally:
            _alembic("upgrade", "head")


def test_0009_downgrade_aborts_before_losing_pre_account_task_context() -> None:
    database_url = require_disposable_postgres()
    workspace_id = str(uuid4())
    lead_id = str(uuid4())
    task_id = str(uuid4())
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, slug, name) "
                    "VALUES (:id, :slug, 'Pre-account migration fixture')"
                ),
                {"id": workspace_id, "slug": f"pre-account-{workspace_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO leads (id, workspace_id) VALUES (:id, :workspace_id)"
                ),
                {"id": lead_id, "workspace_id": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO tasks "
                    "(id, workspace_id, account_id, lead_id, task_type, title, due_at, owner_user_id) "
                    "VALUES (:id, :workspace_id, NULL, :lead_id, 'call', "
                    "'Pre-account task', now(), :owner_user_id)"
                ),
                {
                    "id": task_id,
                    "workspace_id": workspace_id,
                    "lead_id": lead_id,
                    "owner_user_id": str(uuid4()),
                },
            )

        result = _run_alembic("downgrade", "0008")

        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert "cannot downgrade while pre-account tasks exist" in output
        assert task_id not in output
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0009"
            )
            assert (
                connection.execute(
                    text("SELECT lead_id::text FROM tasks WHERE id = :task_id"),
                    {"task_id": task_id},
                ).scalar_one()
                == lead_id
            )
    finally:
        engine.dispose()
        try:
            _delete_task_fixture(database_url, workspace_id)
        finally:
            _alembic("upgrade", "head")


def test_0008_downgrade_aborts_before_losing_structured_outcome() -> None:
    database_url = require_disposable_postgres()
    workspace_id: str | None = None
    activity_id = str(uuid4())
    try:
        _alembic("downgrade", "0007")
        workspace_id, lead_ids, _ = _insert_0007_task_fixture(lead_count=1)
        _alembic("upgrade", "0008")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO activities "
                        "(id, workspace_id, account_id, lead_id, activity_type, "
                        "occurred_at, title, outcome_code) "
                        "SELECT :id, workspace_id, account_id, id, 'call', now(), "
                        "'Call outcome', 'connected' FROM leads WHERE id = :lead_id"
                    ),
                    {"id": activity_id, "lead_id": lead_ids[0]},
                )

            result = _run_alembic("downgrade", "0007")

            assert result.returncode != 0
            output = result.stdout + result.stderr
            assert "cannot safely downgrade pipeline operations" in output
            assert activity_id not in output
            assert _current_revision(database_url) == "0008"
            assert "outcome_code" in {
                column["name"] for column in inspect(engine).get_columns("activities")
            }
        finally:
            engine.dispose()
    finally:
        _reset_database(database_url)
        _alembic("upgrade", "head")


def test_0008_downgrade_aborts_when_operational_task_mapping_is_ambiguous() -> None:
    database_url = require_disposable_postgres()
    workspace_id: str | None = None
    try:
        _alembic("downgrade", "0007")
        workspace_id, lead_ids, task_ids = _insert_0007_task_fixture(lead_count=1)
        _alembic("upgrade", "0008")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO leads (id, workspace_id, account_id) "
                        "SELECT :lead_id, workspace_id, account_id FROM leads "
                        "WHERE id = :existing_lead_id"
                    ),
                    {"lead_id": str(uuid4()), "existing_lead_id": lead_ids[0]},
                )

            result = _run_alembic("downgrade", "0007")

            assert result.returncode != 0
            output = result.stdout + result.stderr
            assert "cannot safely downgrade pipeline operations" in output
            assert all(task_id not in output for task_id in task_ids)
            assert _current_revision(database_url) == "0008"
            assert "lead_id" in {
                column["name"] for column in inspect(engine).get_columns("tasks")
            }
        finally:
            engine.dispose()
    finally:
        try:
            if workspace_id is not None:
                _delete_task_fixture(database_url, workspace_id)
        finally:
            _alembic("upgrade", "head")


def test_0008_downgrade_aborts_for_non_operational_lead_context() -> None:
    database_url = require_disposable_postgres()
    workspace_id: str | None = None
    try:
        _alembic("downgrade", "0007")
        workspace_id, lead_ids, _ = _insert_0007_task_fixture(lead_count=1)
        _alembic("upgrade", "0008")
        task_id = str(uuid4())
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO tasks "
                        "(id, workspace_id, account_id, lead_id, task_type, title, due_at, owner_user_id) "
                        "SELECT :id, workspace_id, account_id, :lead_id, 'review', "
                        "'Review task', now(), :owner_user_id FROM leads WHERE id = :lead_id"
                    ),
                    {
                        "id": task_id,
                        "lead_id": lead_ids[0],
                        "owner_user_id": str(uuid4()),
                    },
                )

            result = _run_alembic("downgrade", "0007")

            assert result.returncode != 0
            output = result.stdout + result.stderr
            assert "cannot safely downgrade pipeline operations" in output
            assert task_id not in output
            assert _current_revision(database_url) == "0008"
        finally:
            engine.dispose()
    finally:
        try:
            if workspace_id is not None:
                _delete_task_fixture(database_url, workspace_id)
        finally:
            _alembic("upgrade", "head")


@pytest.mark.parametrize("invalid_mapping", ["missing", "mismatched"])
def test_0008_downgrade_aborts_when_operational_lead_cannot_be_reconstructed(
    invalid_mapping: str,
) -> None:
    database_url = require_disposable_postgres()
    task_ids: list[str] = []
    try:
        _alembic("downgrade", "0007")
        workspace_id, lead_ids, task_ids = _insert_0007_task_fixture(lead_count=1)
        _alembic("upgrade", "0008")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE tasks DROP CONSTRAINT "
                        "fk_tasks_workspace_account_lead"
                    )
                )
                if invalid_mapping == "missing":
                    connection.execute(
                        text("ALTER TABLE tasks DROP CONSTRAINT ck_tasks_lead_context")
                    )
                    connection.execute(
                        text(
                            "UPDATE tasks SET lead_id = NULL "
                            "WHERE id::text = ANY(:task_ids)"
                        ),
                        {"task_ids": task_ids},
                    )
                    connection.execute(
                        text("DELETE FROM leads WHERE id = :lead_id"),
                        {"lead_id": lead_ids[0]},
                    )
                else:
                    other_account_id = str(uuid4())
                    other_lead_id = str(uuid4())
                    connection.execute(
                        text(
                            "INSERT INTO accounts "
                            "(id, workspace_id, display_name, normalized_name) "
                            "VALUES (:id, :workspace_id, 'Other account', :normalized_name)"
                        ),
                        {
                            "id": other_account_id,
                            "workspace_id": workspace_id,
                            "normalized_name": f"other-{other_account_id}",
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO leads (id, workspace_id, account_id) "
                            "VALUES (:id, :workspace_id, :account_id)"
                        ),
                        {
                            "id": other_lead_id,
                            "workspace_id": workspace_id,
                            "account_id": other_account_id,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE tasks SET lead_id = :lead_id "
                            "WHERE id::text = ANY(:task_ids)"
                        ),
                        {"lead_id": other_lead_id, "task_ids": task_ids},
                    )

            result = _run_alembic("downgrade", "0007")

            assert result.returncode != 0
            output = result.stdout + result.stderr
            assert "cannot safely downgrade pipeline operations" in output
            assert all(task_id not in output for task_id in task_ids)
            assert _current_revision(database_url) == "0008"
            assert "ix_tasks_workspace_lead_status_due" in {
                index["name"] for index in inspect(engine).get_indexes("tasks")
            }
        finally:
            engine.dispose()
    finally:
        _reset_database(database_url)
        _alembic("upgrade", "head")
