from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"


def _alembic(database_url: str, command: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(CONFIG), command, revision],
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "PYTHONPATH": str(ROOT),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def lifecycle_database_url() -> Iterator[str]:
    source_url = require_disposable_postgres()
    parsed = make_url(source_url)
    database_name = f"crm_account_city_migration_test_{uuid4().hex}"
    database_url = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    maintenance_url = parsed.set(database="postgres").render_as_string(
        hide_password=False
    )
    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield database_url
    finally:
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


def test_0010_adds_nullable_nonblank_account_city(
    lifecycle_database_url: str,
) -> None:
    database_url = lifecycle_database_url
    try:
        _alembic(database_url, "upgrade", "0009")
        _alembic(database_url, "upgrade", "0010")

        engine = create_engine(database_url)
        try:
            columns = {
                column["name"] for column in inspect(engine).get_columns("accounts")
            }
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
        _alembic(database_url, "upgrade", "head")
