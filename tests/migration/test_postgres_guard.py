from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from ._postgres import DISPOSABLE_MARKER, require_disposable_postgres


ROOT = Path(__file__).resolve().parents[2]


def test_postgres_guard_rejects_remote_or_non_test_database(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:secret@db.example/production",  # pragma: allowlist secret
    )
    monkeypatch.setenv(DISPOSABLE_MARKER, "1")

    with pytest.raises(pytest.fail.Exception, match="local test database"):
        require_disposable_postgres()


@pytest.mark.parametrize(
    "query",
    [
        "host=db.example",
        "HOST=db.example",
        "ho%73t=db.example",
        "host=localhost&host=db.example",
        "hostaddr=203.0.113.10",
        "port=5433",
        "dbname=production",
        "database=production",
        "user=remote-user",
        "password=remote-password",
        "passfile=%2Ftmp%2Freviewer.pgpass",
        "service=production",
        "servicefile=%2Ftmp%2Fpg_service.conf",
    ],
)
def test_postgres_guard_rejects_libpq_destination_and_identity_overrides(
    monkeypatch, query
):
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+psycopg://local:test@127.0.0.1/local_test?{query}",
    )
    monkeypatch.setenv(DISPOSABLE_MARKER, "1")

    with pytest.raises(pytest.fail.Exception, match="local test database"):
        require_disposable_postgres()


@pytest.mark.parametrize(
    "name",
    (
        "PGDATABASE",
        "PGHOST",
        "PGHOSTADDR",
        "PGPASSFILE",
        "PGPASSWORD",
        "PGPORT",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGUSER",
    ),
)
def test_postgres_guard_rejects_ambient_libpq_overrides(monkeypatch, name):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://local:test@127.0.0.1/local_test",
    )
    monkeypatch.setenv(DISPOSABLE_MARKER, "1")
    monkeypatch.setenv(name, "unsafe-override")

    with pytest.raises(pytest.fail.Exception, match="local test database"):
        require_disposable_postgres()


def test_pytest_session_rejects_unsafe_ambient_database_before_collection():
    unsafe_url = (
        "postgresql+psycopg://unsafe-user:unsafe-password@db.example/production"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/integration/persistence/test_account_constraints.py",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": unsafe_url,
            DISPOSABLE_MARKER: "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    rendered = result.stdout + result.stderr
    assert result.returncode != 0
    assert "PostgreSQL mutation tests require a local test database" in rendered
    assert unsafe_url not in rendered
    assert "unsafe-user" not in rendered
    assert "unsafe-password" not in rendered
