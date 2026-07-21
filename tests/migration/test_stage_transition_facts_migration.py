from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "migrations/alembic.ini"


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


def test_deployed_revision_ids_remain_stable_before_new_operational_migrations() -> (
    None
):
    versions = ROOT / "migrations/versions"
    expected_lineage = {
        "0010_account_city.py": (
            'revision: str = "0010"',
            'down_revision: str | None = "0009"',
        ),
        "0011_pre_account_lead_identity.py": (
            'revision: str = "0011"',
            'down_revision: str | None = "0010"',
        ),
        "0012_activity_stage_transition_facts.py": (
            'revision: str = "0012"',
            'down_revision: str | None = "0011"',
        ),
        "0013_lead_stage_account_requirement.py": (
            'revision: str = "0013"',
            'down_revision: str | None = "0012"',
        ),
    }

    for filename, markers in expected_lineage.items():
        contents = (versions / filename).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in contents


def _temporary_database(prefix: str) -> Iterator[str]:
    source_url = require_disposable_postgres()
    parsed = make_url(source_url)
    database_name = f"{prefix}_{uuid4().hex}"
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


@pytest.fixture(scope="module")
def lifecycle_database_url() -> Iterator[str]:
    yield from _temporary_database("crm_stage_facts_migration_test")


@pytest.fixture
def divergent_staging_database_url() -> Iterator[str]:
    yield from _temporary_database("crm_divergent_staging_migration_test")


def test_populated_0011_upgrade_preserves_unknown_history_and_downgrade_fails_closed(
    lifecycle_database_url: str,
) -> None:
    database_url = lifecycle_database_url
    workspace_id = str(uuid4())
    lead_id = str(uuid4())
    historical_activity_id = str(uuid4())
    structured_activity_id = str(uuid4())
    config = Config(str(CONFIG))
    head_revision = ScriptDirectory.from_config(config).get_current_head()
    assert head_revision is not None and head_revision != "0011"

    _alembic(database_url, "upgrade", "0011")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, slug, name) "
                    "VALUES (:id, :slug, 'Stage facts migration fixture')"
                ),
                {"id": workspace_id, "slug": f"stage-facts-{workspace_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO leads (id, workspace_id) VALUES (:id, :workspace_id)"
                ),
                {"id": lead_id, "workspace_id": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO activities "
                    "(id, workspace_id, lead_id, activity_type, occurred_at, title, "
                    "semantic_fingerprint) VALUES "
                    "(:id, :workspace_id, :lead_id, 'stage_change', now(), "
                    "'Historical stage observation', :fingerprint)"
                ),
                {
                    "id": historical_activity_id,
                    "workspace_id": workspace_id,
                    "lead_id": lead_id,
                    "fingerprint": "0" * 64,
                },
            )

        for _ in range(2):
            _alembic(database_url, "upgrade", "head")
            assert _current_revision(database_url) == head_revision
            assert {"from_stage", "to_stage"}.issubset(
                {column["name"] for column in inspect(engine).get_columns("activities")}
            )
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT from_stage, to_stage FROM activities WHERE id = :id"),
                    {"id": historical_activity_id},
                ).one() == (None, None)
            _alembic(database_url, "downgrade", "0011")
            assert _current_revision(database_url) == "0011"
            assert {"from_stage", "to_stage"}.isdisjoint(
                {column["name"] for column in inspect(engine).get_columns("activities")}
            )

        _alembic(database_url, "upgrade", "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO activities "
                    "(id, workspace_id, lead_id, activity_type, occurred_at, title, "
                    "semantic_fingerprint, from_stage, to_stage) VALUES "
                    "(:id, :workspace_id, :lead_id, 'stage_change', now(), "
                    "'Structured stage transition', :fingerprint, 'new', 'contacted')"
                ),
                {
                    "id": structured_activity_id,
                    "workspace_id": workspace_id,
                    "lead_id": lead_id,
                    "fingerprint": "1" * 64,
                },
            )

        rejected = _run_alembic(database_url, "downgrade", "0011")
        assert rejected.returncode != 0
        output = rejected.stdout + rejected.stderr
        assert "cannot downgrade while structured stage transitions exist" in output
        assert structured_activity_id not in output
        assert _current_revision(database_url) == head_revision
        assert {"from_stage", "to_stage"}.issubset(
            {column["name"] for column in inspect(engine).get_columns("activities")}
        )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT from_stage, to_stage FROM activities WHERE id = :id"),
                {"id": structured_activity_id},
            ).one() == ("new", "contacted")
    finally:
        engine.dispose()


def test_divergent_staging_lineage_can_be_repaired_without_recreating_schema(
    divergent_staging_database_url: str,
) -> None:
    database_url = divergent_staging_database_url
    _alembic(database_url, "upgrade", "0009")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE activities ADD COLUMN from_stage VARCHAR(32)")
            )
            connection.execute(
                text("ALTER TABLE activities ADD COLUMN to_stage VARCHAR(32)")
            )
            connection.execute(
                text(
                    "ALTER TABLE activities ADD CONSTRAINT "
                    "ck_activities_stage_transition_pair CHECK ("
                    "(from_stage IS NULL AND to_stage IS NULL) OR "
                    "(from_stage IS NOT NULL AND to_stage IS NOT NULL))"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE activities ADD CONSTRAINT "
                    "ck_activities_stage_transition_type CHECK ("
                    "activity_type = 'stage_change' OR "
                    "(from_stage IS NULL AND to_stage IS NULL))"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE activities ADD CONSTRAINT "
                    "ck_activities_stage_transition_values CHECK ("
                    "(from_stage IS NULL AND to_stage IS NULL) OR ("
                    "from_stage IN ('new', 'contacted', 'qualified', "
                    "'meeting_booked', 'meeting_held', 'proposal_requested', "
                    "'proposal_sent', 'negotiation', 'won', 'lost', 'not_a_fit') "
                    "AND to_stage IN ('new', 'contacted', 'qualified', "
                    "'meeting_booked', 'meeting_held', 'proposal_requested', "
                    "'proposal_sent', 'negotiation', 'won', 'lost', 'not_a_fit') "
                    "AND from_stage <> to_stage))"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE leads ADD CONSTRAINT "
                    "ck_leads_stage_requires_account CHECK ("
                    "account_id IS NOT NULL OR stage NOT IN ("
                    "'meeting_booked', 'meeting_held', 'proposal_requested', "
                    "'proposal_sent', 'negotiation', 'won'))"
                )
            )
            connection.execute(text("UPDATE alembic_version SET version_num = '0011'"))

        assert _current_revision(database_url) == "0011"
        assert "city" not in {
            column["name"] for column in inspect(engine).get_columns("accounts")
        }
        assert "company_name" not in {
            column["name"] for column in inspect(engine).get_columns("leads")
        }

        _alembic(database_url, "stamp", "0009")
        _alembic(database_url, "upgrade", "head")

        assert _current_revision(database_url) == "0013"
        assert "city" in {
            column["name"] for column in inspect(engine).get_columns("accounts")
        }
        assert {
            "company_name",
            "contact_name",
            "contact_email",
            "contact_phone",
            "city",
        }.issubset({column["name"] for column in inspect(engine).get_columns("leads")})
        assert {
            "ck_activities_stage_transition_pair",
            "ck_activities_stage_transition_type",
            "ck_activities_stage_transition_values",
        }.issubset(
            {
                item["name"]
                for item in inspect(engine).get_check_constraints("activities")
            }
        )
        assert "ck_leads_stage_requires_account" in {
            item["name"] for item in inspect(engine).get_check_constraints("leads")
        }

        _alembic(database_url, "downgrade", "0011")
        assert _current_revision(database_url) == "0011"
        assert {"from_stage", "to_stage"}.isdisjoint(
            {column["name"] for column in inspect(engine).get_columns("activities")}
        )
        assert "city" in {
            column["name"] for column in inspect(engine).get_columns("accounts")
        }
        assert "company_name" in {
            column["name"] for column in inspect(engine).get_columns("leads")
        }
    finally:
        engine.dispose()
