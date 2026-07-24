from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_worker_is_dry_run_by_default_and_never_requires_live_credentials():
    result = _run(
        "crm_worker.py",
        "--workspace-id",
        "00000000-0000-0000-0000-000000000001",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "apply": False,
        "eligible_count": 0,
        "processed_count": 0,
    }
    assert result.stderr == ""


def test_reconcile_dry_run_validates_local_fixture_without_database(tmp_path):
    fixture = tmp_path / "page.json"
    fixture.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "message-1",
                        "thread_id": "thread-1",
                        "occurred_at": "2026-07-16T12:00:00Z",
                    }
                ],
                "next_cursor": "cursor-1",
            }
        )
    )

    result = _run(
        "crm_reconcile.py",
        "--workspace-id",
        "00000000-0000-0000-0000-000000000001",
        "--connector",
        "gmail",
        "--source-scope",
        "mailbox:test",
        "--stream",
        "messages",
        "--fixture",
        str(fixture),
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "apply": False,
        "duplicate_count": 0,
        "event_count": 1,
        "inserted_count": 0,
    }
    assert "message-1" not in result.stdout
    assert "cursor-1" not in result.stdout
    assert result.stderr == ""


def test_entrypoints_fail_closed_when_apply_has_no_database_configuration(tmp_path):
    fixture = tmp_path / "page.json"
    fixture.write_text(json.dumps({"items": [], "next_cursor": "cursor"}))

    for script, arguments in (
        (
            "crm_worker.py",
            (
                "--workspace-id",
                "00000000-0000-0000-0000-000000000001",
                "--apply",
            ),
        ),
        (
            "crm_reconcile.py",
            (
                "--workspace-id",
                "00000000-0000-0000-0000-000000000001",
                "--connector",
                "gmail",
                "--source-scope",
                "mailbox:test",
                "--stream",
                "messages",
                "--fixture",
                str(fixture),
                "--apply",
            ),
        ),
    ):
        result = _run(script, *arguments)
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == "CRM operation unavailable\n"
