from __future__ import annotations

import pytest

from ._postgres import DISPOSABLE_MARKER, require_disposable_postgres


def test_postgres_guard_rejects_remote_or_non_test_database(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:secret@db.example/production",  # pragma: allowlist secret
    )
    monkeypatch.setenv(DISPOSABLE_MARKER, "1")

    with pytest.raises(pytest.fail.Exception, match="local test database"):
        require_disposable_postgres()
