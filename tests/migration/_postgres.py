from __future__ import annotations

import os
from uuid import UUID

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.engine.base import Engine
from sqlalchemy.orm import Session

from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    IngestEvent,
    Lead,
    SourceIdentity,
    SyncCheckpoint,
    Workspace,
)


DISPOSABLE_MARKER = "CRM_DISPOSABLE_TEST_DATABASE"


def require_disposable_postgres() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        pytest.skip("requires disposable PostgreSQL")
    try:
        parsed = make_url(value)
    except Exception:
        pytest.fail("DATABASE_URL is not a valid disposable PostgreSQL URL")
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.database
        or "test" not in parsed.database.lower()
        or os.getenv(DISPOSABLE_MARKER) != "1"
    ):
        pytest.fail(
            f"PostgreSQL mutation tests require a local test database and {DISPOSABLE_MARKER}=1"
        )
    return value


def cleanup_workspace(engine: Engine, workspace_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL session_replication_role = replica"))
        session.execute(delete(Activity).where(Activity.workspace_id == workspace_id))
        session.execute(text("SET LOCAL session_replication_role = origin"))
        for model in (
            Lead,
            Contact,
            Account,
            SourceIdentity,
            IngestEvent,
            SyncCheckpoint,
        ):
            session.execute(delete(model).where(model.workspace_id == workspace_id))
        session.execute(delete(Workspace).where(Workspace.id == workspace_id))
