from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"


def _alembic(command: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(CONFIG), command, revision],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_0010_adds_nullable_nonblank_account_city() -> None:
    database_url = require_disposable_postgres()
    try:
        _alembic("downgrade", "0009")
        _alembic("upgrade", "0010")

        engine = create_engine(database_url)
        try:
            columns = {column["name"] for column in inspect(engine).get_columns("accounts")}
            checks = {
                constraint["name"]
                for constraint in inspect(engine).get_check_constraints("accounts")
            }
            assert "city" in columns
            assert "ck_accounts_city_nonblank" in checks

            workspace_id = uuid4()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, slug, name) "
                        "VALUES (:id, :slug, 'City migration')"
                    ),
                    {"id": workspace_id, "slug": f"city-{workspace_id}"},
                )
                connection.execute(
                    text(
                        "INSERT INTO accounts "
                        "(id, workspace_id, display_name, normalized_name, city) "
                        "VALUES (:id, :workspace_id, 'Lisbon Account', 'lisbon account', 'Lisboa')"
                    ),
                    {"id": uuid4(), "workspace_id": workspace_id},
                )
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO accounts "
                            "(id, workspace_id, display_name, normalized_name, city) "
                            "VALUES (:id, :workspace_id, 'Blank City', 'blank city', '   ')"
                        ),
                        {"id": uuid4(), "workspace_id": workspace_id},
                    )
            except IntegrityError:
                pass
            else:
                raise AssertionError("blank account city must be rejected")
        finally:
            engine.dispose()
    finally:
        _alembic("upgrade", "head")
