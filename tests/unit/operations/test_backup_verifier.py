from __future__ import annotations

from pathlib import Path

import pytest

from scripts.crm_verify_backup import BackupVerificationError, validate_safe_target


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
