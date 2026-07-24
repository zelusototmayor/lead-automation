from __future__ import annotations

import os

from tests.migration._postgres import require_disposable_postgres


def pytest_sessionstart(session) -> None:
    """Fail closed before collection when an ambient database is unsafe."""
    if os.getenv("DATABASE_URL"):
        require_disposable_postgres()
