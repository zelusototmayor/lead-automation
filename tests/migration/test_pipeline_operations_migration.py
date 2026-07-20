from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, inspect

from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"


def _alembic(command: str, revision: str) -> None:
    result = subprocess.run(
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
    assert result.returncode == 0, result.stderr


def test_0008_adds_pipeline_operation_columns() -> None:
    database_url = require_disposable_postgres()
    _alembic("downgrade", "0007")

    _alembic("upgrade", "0008")

    engine = create_engine(database_url)
    try:
        task_columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
        activity_columns = {
            column["name"] for column in inspect(engine).get_columns("activities")
        }
        assert "lead_id" in task_columns
        assert "outcome_code" in activity_columns
        task_foreign_keys = {
            constraint["name"] for constraint in inspect(engine).get_foreign_keys("tasks")
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
