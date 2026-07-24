from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.engine.base import Engine
from sqlalchemy.orm import Session

from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    EmailMessage,
    Evidence,
    IngestEvent,
    Lead,
    Meeting,
    Proposal,
    ProposalFollowup,
    ProposalItem,
    ProposalVersion,
    Recommendation,
    ReconciliationRun,
    ReviewCandidate,
    SourceIdentity,
    SyncCheckpoint,
    Task,
    Workspace,
)


DISPOSABLE_MARKER = "CRM_DISPOSABLE_TEST_DATABASE"
LIBPQ_CONNECTION_OVERRIDES = {
    "database",
    "dbname",
    "host",
    "hostaddr",
    "passfile",
    "password",
    "port",
    "service",
    "servicefile",
    "user",
}
LIBPQ_ENVIRONMENT_OVERRIDES = {
    "PGDATABASE",
    "PGHOST",
    "PGHOSTADDR",
    "PGPASSFILE",
    "PGPASSWORD",
    "PGPORT",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGUSER",
}


def require_disposable_postgres() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        pytest.skip("requires disposable PostgreSQL")
    try:
        parsed = make_url(value)
        query_keys = {
            key.casefold()
            for key, _ in parse_qsl(urlsplit(value).query, keep_blank_values=True)
        }
    except Exception:
        pytest.fail("DATABASE_URL is not a valid disposable PostgreSQL URL")
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.database
        or "test" not in parsed.database.lower()
        or query_keys & LIBPQ_CONNECTION_OVERRIDES
        or any(
            os.getenv(name) not in (None, "") for name in LIBPQ_ENVIRONMENT_OVERRIDES
        )
        or os.getenv(DISPOSABLE_MARKER) != "1"
    ):
        pytest.fail(
            f"PostgreSQL mutation tests require a local test database and {DISPOSABLE_MARKER}=1"
        )
    return value


def cleanup_workspace(engine: Engine, workspace_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL session_replication_role = replica"))
        from src.crm.persistence.models import AuditEvent, OutboxEvent

        session.execute(
            delete(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        )
        session.execute(
            delete(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        session.execute(
            delete(Recommendation).where(Recommendation.workspace_id == workspace_id)
        )
        proposal_ids = select(Proposal.id).where(Proposal.workspace_id == workspace_id)
        version_ids = select(ProposalVersion.id).where(
            ProposalVersion.proposal_id.in_(proposal_ids)
        )
        session.execute(
            delete(ReviewCandidate).where(ReviewCandidate.workspace_id == workspace_id)
        )
        session.execute(
            delete(ProposalFollowup).where(
                ProposalFollowup.proposal_id.in_(proposal_ids)
            )
        )
        session.execute(
            delete(ProposalItem).where(
                ProposalItem.proposal_version_id.in_(version_ids)
            )
        )
        session.execute(delete(Task).where(Task.workspace_id == workspace_id))
        session.execute(delete(Meeting).where(Meeting.workspace_id == workspace_id))
        session.execute(
            delete(EmailMessage).where(EmailMessage.workspace_id == workspace_id)
        )
        session.execute(
            delete(ReconciliationRun).where(
                ReconciliationRun.workspace_id == workspace_id
            )
        )
        session.execute(
            delete(ProposalVersion).where(ProposalVersion.proposal_id.in_(proposal_ids))
        )
        session.execute(delete(Proposal).where(Proposal.workspace_id == workspace_id))
        session.execute(delete(Evidence).where(Evidence.workspace_id == workspace_id))
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
