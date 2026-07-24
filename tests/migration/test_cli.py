from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from scripts import crm_backfill_accounts, crm_compare_legacy, crm_snapshot_sheets
from src.crm.migration.sheets_snapshot import save_snapshot
from src.crm.persistence.models import Account, Activity, Contact, Lead, Workspace

from ._postgres import cleanup_workspace, require_disposable_postgres
from .test_account_backfill import fixture_snapshot


ROOT = Path(__file__).resolve().parents[2]


def test_backfill_cli_defaults_to_dry_run(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(fixture_snapshot(), snapshot_path)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/crm_backfill_accounts.py",
            "--snapshot",
            str(snapshot_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["applied"] is False
    assert report["imported"] == 4


def test_backfill_cli_accepts_plan_fixture_command_as_explicit_dry_run():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/crm_backfill_accounts.py",
            "--fixture",
            "tests/fixtures/pt_logistics_rows.json",
            "--dry-run",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["applied"] is False
    assert report["imported"] == 4


def test_backfill_cli_environment_url_cannot_authorize_apply(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(fixture_snapshot(), snapshot_path)
    secret_url = "postgresql+psycopg://secret-user:secret-password@db.example/crm"  # pragma: allowlist secret

    result = subprocess.run(
        [
            sys.executable,
            "scripts/crm_backfill_accounts.py",
            "--snapshot",
            str(snapshot_path),
            "--apply",
        ],
        cwd=ROOT,
        env={"DATABASE_URL": secret_url},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "explicit PostgreSQL database_url" in result.stderr
    assert secret_url not in result.stdout + result.stderr
    assert "secret-password" not in result.stdout + result.stderr


def test_backfill_cli_redacts_database_url_from_failures(tmp_path, monkeypatch, capsys):
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(fixture_snapshot(), snapshot_path)
    secret_url = "postgresql+psycopg://secret-user:secret-password@db.example/crm"  # pragma: allowlist secret

    def fail(*args, **kwargs):
        raise ValueError(f"connection failed for {secret_url}")

    monkeypatch.setattr(crm_backfill_accounts, "backfill_accounts", fail)
    result = crm_backfill_accounts.main(
        [
            "--snapshot",
            str(snapshot_path),
            "--apply",
            "--database-url",
            secret_url,
            "--workspace-id",
            "00000000-0000-0000-0000-000000000001",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "backfill failed" in captured.err
    assert secret_url not in captured.out + captured.err
    assert "secret-password" not in captured.out + captured.err


def test_snapshot_cli_defaults_to_report_only_without_writing_file(tmp_path, capsys):
    output_path = tmp_path / "snapshot.json"

    class Source:
        def read_values(self, spreadsheet_id, sheet_name):
            return [["ID", "Status"], ["lead-1", "Meeting Booked"]]

    result = crm_snapshot_sheets.main(
        [
            "--credentials-file",
            "unused.json",
            "--spreadsheet-id",
            "sheet-1",
            "--sheet-name",
            "Leads",
            "--stable-id-column",
            "ID",
            "--output",
            str(output_path),
        ],
        source_factory=lambda _: Source(),
    )

    assert result == 0
    assert not output_path.exists()
    assert json.loads(capsys.readouterr().out) == {
        "conflicts": 0,
        "duplicates": 0,
        "input_rows": 1,
        "saved": False,
        "snapshot_rows": 1,
    }


def test_snapshot_cli_saves_only_with_explicit_save(tmp_path, capsys):
    output_path = tmp_path / "snapshot.json"

    class Source:
        def read_values(self, spreadsheet_id, sheet_name):
            return [["ID", "Status"], ["lead-1", "Meeting Booked"]]

    result = crm_snapshot_sheets.main(
        [
            "--credentials-file",
            "unused.json",
            "--spreadsheet-id",
            "sheet-1",
            "--sheet-name",
            "Leads",
            "--stable-id-column",
            "ID",
            "--output",
            str(output_path),
            "--save",
        ],
        source_factory=lambda _: Source(),
    )

    assert result == 0
    assert output_path.exists()
    assert json.loads(capsys.readouterr().out)["saved"] is True


def test_snapshot_cli_uses_only_explicit_fallback_identity_groups(tmp_path, capsys):
    output_path = tmp_path / "snapshot.json"

    class Source:
        def read_values(self, spreadsheet_id, sheet_name):
            return [
                ["ID", "Company", "Contact", "Email"],
                ["", "Acme", "Ana", "ana@example.com"],
            ]

    result = crm_snapshot_sheets.main(
        [
            "--credentials-file",
            "unused.json",
            "--spreadsheet-id",
            "sheet-1",
            "--sheet-name",
            "Leads",
            "--stable-id-column",
            "ID",
            "--fallback-identity",
            "Email",
            "--fallback-identity",
            "Company,Contact",
            "--output",
            str(output_path),
            "--save",
        ],
        source_factory=lambda _: Source(),
    )

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["snapshot_rows"] == 1
    payload = json.loads(output_path.read_text())
    assert payload["rows"][0]["external_id"].startswith("derived:")


def test_snapshot_cli_redacts_unexpected_source_failures(tmp_path, capsys):
    secret = "private-person@example.test"  # pragma: allowlist secret

    class Source:
        def read_values(self, spreadsheet_id, sheet_name):
            raise RuntimeError(secret)

    result = crm_snapshot_sheets.main(
        [
            "--credentials-file",
            "credential-secret.json",
            "--spreadsheet-id",
            "sheet-secret",
            "--sheet-name",
            "Leads",
            "--stable-id-column",
            "ID",
            "--output",
            str(tmp_path / "snapshot.json"),
        ],
        source_factory=lambda _: Source(),
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err == "error: snapshot failed; check explicit arguments\n"
    assert secret not in captured.out + captured.err


def test_compare_cli_redacts_unexpected_failures(monkeypatch, capsys):
    secret_url = "postgresql+psycopg://person:password@private.example/crm"  # pragma: allowlist secret

    def fail(*args, **kwargs):
        raise RuntimeError(secret_url)

    monkeypatch.setattr(crm_compare_legacy, "load_snapshot", lambda _: object())
    monkeypatch.setattr(crm_compare_legacy, "compare_legacy", fail)
    result = crm_compare_legacy.main(
        [
            "--snapshot",
            "private-file.json",
            "--database-url",
            secret_url,
            "--workspace-id",
            "00000000-0000-0000-0000-000000000001",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err == "error: comparison failed; check explicit arguments\n"
    assert secret_url not in captured.out + captured.err


def test_backfill_cli_postgres_apply_and_replay_are_idempotent(tmp_path):
    database_url = require_disposable_postgres()
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(fixture_snapshot(), snapshot_path)
    workspace_id = uuid4()
    engine = create_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id, slug=f"cli-{workspace_id}", name="CLI Fixture"
                )
            )

        command = [
            sys.executable,
            "scripts/crm_backfill_accounts.py",
            "--snapshot",
            str(snapshot_path),
            "--apply",
            "--database-url",
            database_url,
            "--workspace-id",
            str(workspace_id),
        ]
        first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        replay = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

        assert first.returncode == 0, first.stderr
        assert replay.returncode == 0, replay.stderr
        assert json.loads(first.stdout)["imported"] == 4
        assert json.loads(replay.stdout)["replay_noop"] == 4
        assert (
            database_url
            not in first.stdout + first.stderr + replay.stdout + replay.stderr
        )
        with Session(engine) as session:
            counts = tuple(
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
                for model in (Account, Contact, Lead, Activity)
            )
        assert counts == (3, 3, 4, 4)
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()
