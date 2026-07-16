from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.crm.migration.backfill import backfill_accounts
from src.crm.migration.compare import compare_legacy
from src.crm.migration.sheets_snapshot import save_snapshot
from src.crm.persistence.models import Account, Lead, SourceIdentity, Workspace

from ._postgres import cleanup_workspace, require_disposable_postgres
from .test_account_backfill import fixture_snapshot


ROOT = Path(__file__).resolve().parents[2]


def test_compare_reports_redacted_postgres_parity(tmp_path):
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    engine = create_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"compare-{workspace_id}",
                    name="Compare Fixture",
                )
            )
        backfill_accounts(
            fixture_snapshot(),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )

        report = compare_legacy(
            fixture_snapshot(), database_url=database_url, workspace_id=workspace_id
        )

        assert report.safe_dict() == {
            "input_rows": 7,
            "snapshot_rows": 5,
            "expected_imports": 4,
            "matched_leads": 4,
            "matched_accounts": 3,
            "missing_leads": 0,
            "missing_accounts": 0,
            "extra_leads": 0,
            "stage_mismatches": 0,
            "account_association_mismatches": 0,
            "source_field_mismatches": 0,
            "account_state_mismatches": 0,
            "duplicates": 1,
            "conflicts": 0,
            "unmapped_stages": {"unmapped": 1},
            "parity": False,
        }
        serialized = str(report.safe_dict())
        assert "Northwind" not in serialized
        assert "alex@" not in serialized
        assert "lead-meeting" not in serialized

        snapshot_path = tmp_path / "snapshot.json"
        save_snapshot(fixture_snapshot(), snapshot_path)
        cli = subprocess.run(
            [
                sys.executable,
                "scripts/crm_compare_legacy.py",
                "--snapshot",
                str(snapshot_path),
                "--database-url",
                database_url,
                "--workspace-id",
                str(workspace_id),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert cli.returncode == 1, cli.stderr
        assert json.loads(cli.stdout)["parity"] is False
        assert database_url not in cli.stdout + cli.stderr
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_compare_marks_material_stage_account_and_source_mismatches_without_row_data():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    engine = create_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"mismatch-{workspace_id}",
                    name="Mismatch Fixture",
                )
            )
        backfill_accounts(
            fixture_snapshot(),
            apply=True,
            database_url=database_url,
            workspace_id=workspace_id,
        )
        with Session(engine) as session, session.begin():
            identities = session.scalars(
                select(SourceIdentity).where(
                    SourceIdentity.workspace_id == workspace_id,
                    SourceIdentity.entity_kind == "lead",
                )
            ).all()
            leads_by_external_id = {
                identity.external_id: session.get(Lead, identity.canonical_entity_id)
                for identity in identities
            }
            leads_by_external_id["lead-contacted"].stage = "new"
            leads_by_external_id["homonym-one"].source_origin = "different source"
            meeting_account = session.get(
                Account, leads_by_external_id["lead-meeting"].account_id
            )
            meeting_account.lifecycle_stage = "potential"
            meeting_account.highest_stage_rank = 0
            next(
                identity
                for identity in identities
                if identity.external_id == "homonym-two"
            ).canonical_entity_id = leads_by_external_id["lead-contacted"].id

        report = compare_legacy(
            fixture_snapshot(), database_url=database_url, workspace_id=workspace_id
        )

        assert report.stage_mismatches == 2
        assert report.account_association_mismatches == 1
        assert report.source_field_mismatches == 1
        assert report.account_state_mismatches == 1
        assert report.parity is False
        serialized = json.dumps(report.safe_dict())
        assert "different source" not in serialized
        assert "lead-" not in serialized
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def test_compare_detects_source_scoped_leads_missing_from_current_snapshot():
    database_url = require_disposable_postgres()
    workspace_id = uuid4()
    engine = create_engine(database_url)
    full = fixture_snapshot()
    reduced = type(full)(
        full.spreadsheet_id,
        full.sheet_name,
        full.stable_id_column,
        full.input_rows,
        tuple(row for row in full.rows if row.external_id != "lead-contacted"),
        full.duplicate_ids,
        full.missing_id_rows,
    )
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"deletion-{workspace_id}",
                    name="Deletion Fixture",
                )
            )
        backfill_accounts(
            full, apply=True, database_url=database_url, workspace_id=workspace_id
        )

        report = compare_legacy(
            reduced, database_url=database_url, workspace_id=workspace_id
        )

        assert report.extra_leads == 1
        assert report.parity is False
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()
