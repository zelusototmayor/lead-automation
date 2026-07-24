from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.crm.persistence.models import (
    Account,
    Evidence,
    Proposal,
    ProposalVersion,
    ReviewCandidate,
    SourceIdentity,
    Workspace,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.evidence_service import EvidenceService, RecordEvidenceCommand
from src.crm.services.proposal_discovery_service import (
    DiscoverProposalCommand,
    ProposalDiscoveryService,
)
from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "migrations/alembic.ini"
NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine():
    url = require_disposable_postgres()

    def alembic(*arguments):
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(CONFIG), *arguments],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
        )

    result = alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    value = create_engine(url)
    try:
        yield value
    finally:
        value.dispose()


def graph(engine, slug):
    workspace_id, account_id, message_id, thread_id = uuid4(), uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=workspace_id, slug=slug, name=slug))
        session.flush()
        session.add(
            Account(
                id=account_id,
                workspace_id=workspace_id,
                display_name=slug,
                normalized_name=slug,
            )
        )
        session.flush()
        session.add_all(
            [
                SourceIdentity(
                    id=message_id,
                    workspace_id=workspace_id,
                    source_system="gmail",
                    entity_kind="message",
                    source_scope="mailbox:test",
                    external_id=f"message:{slug}",
                ),
                SourceIdentity(
                    id=thread_id,
                    workspace_id=workspace_id,
                    source_system="gmail",
                    entity_kind="thread",
                    source_scope="mailbox:test",
                    external_id=f"thread:{slug}",
                ),
            ]
        )
    return workspace_id, account_id, message_id, thread_id


def test_migration_exposes_evidence_review_and_thread_constraints(engine):
    inspector = inspect(engine)
    assert {"evidence", "review_candidates"} <= set(inspector.get_table_names())
    assert "thread_source_identity_id" in {
        column["name"] for column in inspector.get_columns("proposals")
    }
    assert "fk_proposals_workspace_sent_evidence" in {
        fk["name"] for fk in inspector.get_foreign_keys("proposals")
    }
    assert "fk_proposal_versions_source_document_evidence_id_evidence" in {
        fk["name"] for fk in inspector.get_foreign_keys("proposal_versions")
    }


def test_evidence_is_idempotent_append_only_and_caller_transactional(engine):
    workspace_id, account_id, message_id, _ = graph(engine, f"evidence-{uuid4().hex}")
    factory = sessionmaker(engine, expire_on_commit=False)
    command = RecordEvidenceCommand(
        workspace_id=workspace_id,
        account_id=account_id,
        source_identity_id=message_id,
        evidence_type="email_message",
        content_hash="a" * 64,
        captured_at=NOW,
    )
    with SqlAlchemyUnitOfWork(factory) as uow:
        first = EvidenceService(uow).record(command)
        second = EvidenceService(uow).record(command)
        assert first.id == second.id
    with Session(engine) as session:
        assert (
            session.scalar(
                select(Evidence).where(Evidence.workspace_id == workspace_id)
            )
            is None
        )

    with SqlAlchemyUnitOfWork(factory) as uow:
        evidence = EvidenceService(uow).record(command)
        evidence_id = evidence.id
        uow.commit()
    with pytest.raises(DBAPIError), Session(engine) as session, session.begin():
        session.execute(
            update(Evidence)
            .where(Evidence.id == evidence_id)
            .values(sensitivity="public")
        )


def test_discovery_persists_revision_review_and_tenant_safe_provenance(engine):
    workspace_id, account_id, message_id, thread_id = graph(
        engine, f"discovery-{uuid4().hex}"
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    with SqlAlchemyUnitOfWork(factory) as uow:
        first = ProposalDiscoveryService(uow).discover(
            DiscoverProposalCommand(
                workspace_id=workspace_id,
                account_id=account_id,
                message_source_identity_id=message_id,
                thread_source_identity_id=thread_id,
                occurred_at=NOW,
                direction="outbound",
                subject="Proposal",
                classification="sent_attachment",
                attachment_name="proposal.pdf",
                attachment_content_hash="b" * 64,
                currency="EUR",
                one_off_amount=Decimal("2500.00"),
            )
        )
        assert first.proposal.value_state == "candidate"
        uow.commit()
    with Session(engine) as session:
        proposal = session.scalar(
            select(Proposal).where(Proposal.workspace_id == workspace_id)
        )
        version = session.scalar(
            select(ProposalVersion).where(ProposalVersion.proposal_id == proposal.id)
        )
        assert proposal.sent_evidence_id is not None
        assert version.source_document_evidence_id is not None

    other_workspace, other_account, other_message, _ = graph(
        engine, f"other-{uuid4().hex}"
    )
    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        other_evidence = Evidence(
            workspace_id=other_workspace,
            account_id=other_account,
            source_identity_id=other_message,
            evidence_type="attachment",
            content_hash="c" * 64,
            captured_at=NOW,
        )
        session.add(other_evidence)
        session.flush()
        session.add(
            ReviewCandidate(
                workspace_id=workspace_id,
                account_id=account_id,
                proposal_id=proposal.id,
                evidence_id=other_evidence.id,
                action_type="review_proposal_value",
                dedupe_key="cross-tenant",
            )
        )
        session.flush()
