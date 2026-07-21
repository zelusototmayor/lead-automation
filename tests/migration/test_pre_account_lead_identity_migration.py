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

from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"


def _run_alembic(
    database_url: str, command: str, revision: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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


def _alembic(database_url: str, command: str, revision: str) -> None:
    result = _run_alembic(database_url, command, revision)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def lifecycle_database_url() -> Iterator[str]:
    source_url = require_disposable_postgres()
    parsed = make_url(source_url)
    database_name = f"crm_pre_account_migration_test_{uuid4().hex}"
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


def test_0011_adds_pre_account_lead_identity_fields(
    lifecycle_database_url: str,
) -> None:
    database_url = lifecycle_database_url
    try:
        _alembic(database_url, "upgrade", "0010")
        _alembic(database_url, "upgrade", "0011")
        engine = create_engine(database_url)
        try:
            columns = {
                column["name"] for column in inspect(engine).get_columns("leads")
            }
            checks = {
                constraint["name"]
                for constraint in inspect(engine).get_check_constraints("leads")
            }
            assert {
                "company_name",
                "contact_name",
                "contact_email",
                "contact_phone",
                "city",
            } <= columns
            assert "ck_leads_company_name_nonblank" in checks
            assert "ck_leads_contact_email_nonblank" in checks
            assert "ck_leads_city_nonblank" in checks
        finally:
            engine.dispose()
    finally:
        _alembic(database_url, "upgrade", "head")


def test_0011_preserves_populated_predecessor_and_round_trips_when_fields_are_null(
    lifecycle_database_url: str,
) -> None:
    database_url = lifecycle_database_url
    workspace_id, lead_id = uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        _alembic(database_url, "upgrade", "0010")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, slug, name) "
                    "VALUES (:id, :slug, 'Pre-account predecessor')"
                ),
                {"id": workspace_id, "slug": f"pre-account-{workspace_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO leads (id, workspace_id) VALUES (:id, :workspace_id)"
                ),
                {"id": lead_id, "workspace_id": workspace_id},
            )

        _alembic(database_url, "upgrade", "0011")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT company_name, contact_name, contact_email, contact_phone, city "
                    "FROM leads WHERE id = :id"
                ),
                {"id": lead_id},
            ).one()
            assert tuple(row) == (None, None, None, None, None)

        _alembic(database_url, "downgrade", "0010")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM leads WHERE id = :id"), {"id": lead_id}
                )
                == 1
            )
        _alembic(database_url, "upgrade", "0011")
    finally:
        _alembic(database_url, "upgrade", "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM leads WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id}
            )
        engine.dispose()


def test_0011_downgrade_fails_closed_without_dropping_populated_identity(
    lifecycle_database_url: str,
) -> None:
    database_url = lifecycle_database_url
    workspace_id, lead_id = uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        _alembic(database_url, "upgrade", "0011")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, slug, name) "
                    "VALUES (:id, :slug, 'Pre-account downgrade')"
                ),
                {"id": workspace_id, "slug": f"pre-account-down-{workspace_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO leads (id, workspace_id, company_name) "
                    "VALUES (:id, :workspace_id, 'Preserved Company')"
                ),
                {"id": lead_id, "workspace_id": workspace_id},
            )

        result = _run_alembic(database_url, "downgrade", "0010")

        assert result.returncode != 0
        assert "Preserved Company" not in result.stderr
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0011"
            )
            assert (
                connection.scalar(
                    text("SELECT company_name FROM leads WHERE id = :id"),
                    {"id": lead_id},
                )
                == "Preserved Company"
            )
    finally:
        _alembic(database_url, "upgrade", "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM leads WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id}
            )
        engine.dispose()
