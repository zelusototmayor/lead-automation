from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.crm.persistence.models import Lead
from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"
CONSTRAINT_NAME = "ck_leads_stage_requires_account"
ACCOUNT_REQUIRED_STAGES = (
    "meeting_booked",
    "meeting_held",
    "proposal_requested",
    "proposal_sent",
    "negotiation",
    "won",
)


def _run_alembic(
    database_url: str, command: str, revision: str
) -> subprocess.CompletedProcess[str]:
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
        env={**os.environ, "DATABASE_URL": database_url, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )


def _alembic(database_url: str, command: str, revision: str) -> None:
    result = _run_alembic(database_url, command, revision)
    assert result.returncode == 0, result.stdout + result.stderr


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        if not inspect(engine).has_table("alembic_version"):
            return None
        with engine.connect() as connection:
            return connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


@pytest.fixture
def lifecycle_database_url() -> Iterator[str]:
    source_url = require_disposable_postgres()
    parsed = make_url(source_url)
    database_name = f"crm_lead_stage_constraint_test_{uuid4().hex}"
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
        assert _current_revision(database_url) is None
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


def _seed_workspace(connection, workspace_id: str) -> None:
    connection.execute(
        text(
            "INSERT INTO workspaces (id, slug, name) "
            "VALUES (:id, :slug, 'Lead stage constraint fixture')"
        ),
        {"id": workspace_id, "slug": f"lead-stage-{workspace_id}"},
    )


def test_model_metadata_mirrors_account_required_stage_constraint() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Lead.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }

    assert CONSTRAINT_NAME in checks
    expression = checks[CONSTRAINT_NAME]
    assert "account_id IS NOT NULL" in expression
    for stage in ACCOUNT_REQUIRED_STAGES:
        assert stage in expression


def test_populated_upgrade_fails_closed_without_leaking_violating_ids(
    lifecycle_database_url: str,
) -> None:
    database_url = lifecycle_database_url
    workspace_id = str(uuid4())
    lead_id = str(uuid4())
    _alembic(database_url, "upgrade", "0010")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _seed_workspace(connection, workspace_id)
            connection.execute(
                text(
                    "INSERT INTO leads (id, workspace_id, stage, highest_stage_rank) "
                    "VALUES (:id, :workspace_id, 'negotiation', 80)"
                ),
                {"id": lead_id, "workspace_id": workspace_id},
            )

        rejected = _run_alembic(database_url, "upgrade", "head")
        assert rejected.returncode != 0
        output = rejected.stdout + rejected.stderr
        assert (
            "cannot enforce account-required lead stages while violations exist"
            in output
        )
        assert lead_id not in output
        assert workspace_id not in output
        assert _current_revision(database_url) == "0010"
        assert CONSTRAINT_NAME not in {
            item["name"] for item in inspect(engine).get_check_constraints("leads")
        }
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM leads WHERE id = :id"), {"id": lead_id}
                )
                == 1
            )
    finally:
        engine.dispose()


def test_valid_populated_upgrade_and_constraint_lifecycle(
    lifecycle_database_url: str,
) -> None:
    database_url = lifecycle_database_url
    workspace_id = str(uuid4())
    account_id = str(uuid4())
    accountless_lead_id = str(uuid4())
    linked_lead_id = str(uuid4())
    config = Config(str(CONFIG))
    head_revision = ScriptDirectory.from_config(config).get_current_head()
    assert head_revision == "0011"

    _alembic(database_url, "upgrade", "0010")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _seed_workspace(connection, workspace_id)
            connection.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, workspace_id, display_name, normalized_name) "
                    "VALUES (:id, :workspace_id, 'Linked', :normalized_name)"
                ),
                {
                    "id": account_id,
                    "workspace_id": workspace_id,
                    "normalized_name": f"linked-{account_id}",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO leads (id, workspace_id, stage, highest_stage_rank) "
                    "VALUES (:accountless_id, :workspace_id, 'qualified', 30), "
                    "(:linked_id, :workspace_id, 'meeting_booked', 40)"
                ),
                {
                    "accountless_id": accountless_lead_id,
                    "linked_id": linked_lead_id,
                    "workspace_id": workspace_id,
                },
            )
            connection.execute(
                text("UPDATE leads SET account_id = :account_id WHERE id = :linked_id"),
                {"account_id": account_id, "linked_id": linked_lead_id},
            )

        for _ in range(2):
            _alembic(database_url, "upgrade", "head")
            assert _current_revision(database_url) == head_revision
            assert CONSTRAINT_NAME in {
                item["name"] for item in inspect(engine).get_check_constraints("leads")
            }
            with engine.connect() as connection:
                stage, persisted_account_id = connection.execute(
                    text("SELECT stage, account_id FROM leads WHERE id = :id"),
                    {"id": linked_lead_id},
                ).one()
                assert stage == "meeting_booked"
                assert str(persisted_account_id) == account_id
            _alembic(database_url, "downgrade", "0010")
            assert _current_revision(database_url) == "0010"
            assert CONSTRAINT_NAME not in {
                item["name"] for item in inspect(engine).get_check_constraints("leads")
            }

        _alembic(database_url, "upgrade", "head")
        for stage in ACCOUNT_REQUIRED_STAGES:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO leads (id, workspace_id, stage) "
                            "VALUES (:id, :workspace_id, :stage)"
                        ),
                        {
                            "id": str(uuid4()),
                            "workspace_id": workspace_id,
                            "stage": stage,
                        },
                    )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE leads SET stage = 'won' WHERE id = :id"),
                    {"id": accountless_lead_id},
                )

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT stage, account_id FROM leads WHERE id = :id"),
                {"id": accountless_lead_id},
            ).one() == ("qualified", None)
    finally:
        engine.dispose()
