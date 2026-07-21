from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text

from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"


def _run_alembic(command: str, revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(CONFIG), command, revision],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )


def _alembic(command: str, revision: str) -> None:
    result = _run_alembic(command, revision)
    assert result.returncode == 0, result.stderr


def test_0011_adds_pre_account_lead_identity_fields() -> None:
    database_url = require_disposable_postgres()
    try:
        _alembic("downgrade", "0010")
        _alembic("upgrade", "0011")
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
        _alembic("upgrade", "head")


def test_0011_preserves_populated_predecessor_and_round_trips_when_fields_are_null() -> (
    None
):
    database_url = require_disposable_postgres()
    workspace_id, lead_id = uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        _alembic("downgrade", "0010")
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

        _alembic("upgrade", "0011")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT company_name, contact_name, contact_email, contact_phone, city "
                    "FROM leads WHERE id = :id"
                ),
                {"id": lead_id},
            ).one()
            assert tuple(row) == (None, None, None, None, None)

        _alembic("downgrade", "0010")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM leads WHERE id = :id"), {"id": lead_id}
                )
                == 1
            )
        _alembic("upgrade", "0011")
    finally:
        _alembic("upgrade", "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM leads WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id}
            )
        engine.dispose()


def test_0011_downgrade_fails_closed_without_dropping_populated_identity() -> None:
    database_url = require_disposable_postgres()
    workspace_id, lead_id = uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        _alembic("upgrade", "0011")
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

        result = _run_alembic("downgrade", "0010")

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
        _alembic("upgrade", "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM leads WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id}
            )
        engine.dispose()
