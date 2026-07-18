from __future__ import annotations

from pathlib import Path

import pytest

from scripts.crm_verify_backup import (
    BackupVerificationError,
    EXPECTED_SCHEMA_REVISION,
    REQUIRED_CONSTRAINTS,
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    validate_safe_target,
)


def test_backup_verifier_contract_tracks_canonical_engagement_schema():
    assert EXPECTED_SCHEMA_REVISION == "0007"
    assert {
        "email_messages",
        "meetings",
        "tasks",
        "reconciliation_runs",
    } <= REQUIRED_TABLES
    assert {
        "fk_email_messages_workspace_account_evidence",
        "fk_meetings_workspace_account_notes_evidence",
        "ck_email_messages_mailbox_identity",
        "ck_reconciliation_runs_report_minimized",
    } <= REQUIRED_CONSTRAINTS
    assert {
        "uq_email_messages_workspace_mailbox_provider",
        "uq_meetings_workspace_provider_occurrence",
        "ix_tasks_account_status_due",
    } <= REQUIRED_INDEXES


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://operator@db.example/crm_test",
        "postgresql://operator@127.0.0.1/production",
        "postgresql://operator@127.0.0.1/crm_test?host=db.example",
        "mysql://operator@127.0.0.1/crm_test",
    ],
)
def test_backup_verifier_rejects_targets_that_are_not_explicitly_disposable(url):
    with pytest.raises(BackupVerificationError, match="unsafe verification target"):
        validate_safe_target(url, disposable_marker=True)


def test_backup_verifier_requires_explicit_disposable_marker():
    with pytest.raises(BackupVerificationError, match="unsafe verification target"):
        validate_safe_target(
            "postgresql://operator@127.0.0.1/crm_task17_test",
            disposable_marker=False,
        )


def test_backup_verifier_accepts_only_custom_format_dump(tmp_path: Path):
    from scripts.crm_verify_backup import validate_backup_file

    plain_sql = tmp_path / "backup.sql"
    plain_sql.write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="invalid backup input"):
        validate_backup_file(plain_sql)

    custom = tmp_path / "backup.dump"
    custom.write_bytes(b"PGDMP" + b"\x00" * 64)
    assert validate_backup_file(custom) == custom.resolve()


def test_backup_verifier_ignores_ambient_libpq_destination_overrides(monkeypatch):
    for name, value in {
        "PGHOSTADDR": "203.0.113.77",
        "PGSERVICE": "hostile-service",
        "PGSERVICEFILE": "/tmp/hostile.conf",
        "PGOPTIONS": "-c search_path=hostile",
    }.items():
        monkeypatch.setenv(name, value)

    target = validate_safe_target(
        "postgresql://operator:secret@localhost:55432/crm_restore_test",
        disposable_marker=True,
    )

    assert target.connection_kwargs()["hostaddr"] == "127.0.0.1"
    environment = target.subprocess_environment("crm_restore_verify_" + "a" * 32)
    assert environment == {
        "PGHOST": "localhost",
        "PGHOSTADDR": "127.0.0.1",
        "PGPORT": "55432",
        "PGUSER": "operator",
        "PGDATABASE": "crm_restore_verify_" + "a" * 32,
        "PGPASSWORD": "secret",
    }
