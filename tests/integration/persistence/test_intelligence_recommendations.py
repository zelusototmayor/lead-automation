from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.crm.persistence.models import (
    Account,
    Activity,
    Evidence,
    Proposal,
    Recommendation,
    ReviewCandidate,
    SourceIdentity,
    Workspace,
)
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.intelligence_service import RecommendationService
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


def test_refresh_materializes_supported_rules_dedupes_and_leaves_commit_to_caller():
    engine = create_engine(require_disposable_postgres())
    factory = sessionmaker(engine, expire_on_commit=False)
    workspace_id, account_id, proposal_id = uuid4(), uuid4(), uuid4()
    source_id, evidence_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(id=workspace_id, slug=f"rules-{workspace_id}", name="Rules")
            )
            session.flush()
            session.add(
                Account(
                    id=account_id,
                    workspace_id=workspace_id,
                    display_name="Acme",
                    normalized_name="acme",
                )
            )
            session.flush()
            session.add(
                SourceIdentity(
                    id=source_id,
                    workspace_id=workspace_id,
                    source_system="gmail",
                    entity_kind="message",
                    source_scope="fixture",
                    external_id="message-1",
                )
            )
            session.flush()
            session.add(
                Evidence(
                    id=evidence_id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    source_identity_id=source_id,
                    evidence_type="email_message",
                    content_hash="a" * 64,
                    captured_at=now - timedelta(days=20),
                )
            )
            session.add_all(
                [
                    Activity(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        activity_type="meeting",
                        occurred_at=now - timedelta(days=2),
                        title="Held meeting",
                    ),
                    Activity(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        activity_type="email_received",
                        direction="inbound",
                        occurred_at=now - timedelta(hours=2),
                        title="Inbound",
                    ),
                    Proposal(
                        id=proposal_id,
                        workspace_id=workspace_id,
                        account_id=account_id,
                        title="Proposal",
                        currency="EUR",
                        status="sent",
                        sent_at=now - timedelta(days=20),
                        sent_verification_state="legacy_unverified",
                    ),
                ]
            )
            session.flush()
            session.add(
                ReviewCandidate(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    evidence_id=evidence_id,
                    action_type="send_promised_proposal",
                    state="open",
                    dedupe_key="promise:1",
                )
            )

        with SqlAlchemyUnitOfWork(factory) as uow:
            rows = RecommendationService(uow).refresh(workspace_id, now=now)
            assert {row.rule_code for row in rows} == {
                "held_meeting_without_notes",
                "meeting_without_calendar_event",
                "inbound_awaiting_response",
                "proposal_missing_next_action",
                "proposal_stale",
                "promised_proposal_not_sent",
            }
            RecommendationService(uow).refresh(workspace_id, now=now)

        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count(Recommendation.id)).where(
                        Recommendation.workspace_id == workspace_id
                    )
                )
                == 0
            )

        with SqlAlchemyUnitOfWork(factory) as uow:
            RecommendationService(uow).refresh(workspace_id, now=now)
            RecommendationService(uow).refresh(workspace_id, now=now)
            uow.commit()

        with Session(engine) as session:
            recommendations = session.scalars(
                select(Recommendation).where(
                    Recommendation.workspace_id == workspace_id
                )
            ).all()
            assert len(recommendations) == 6
            assert all(
                row.evidence_json and row.state == "open" for row in recommendations
            )
            assert len({row.dedupe_key for row in recommendations}) == 6
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


@pytest.mark.parametrize("workspace_id", [None, "not-a-uuid"])
def test_refresh_rejects_invalid_workspace_with_generic_error(workspace_id):
    from src.crm.services.intelligence_service import IntelligenceUnavailable

    with pytest.raises(IntelligenceUnavailable, match="^intelligence unavailable$"):
        RecommendationService(object()).refresh(workspace_id)


def test_database_rejects_empty_evidence_and_duplicate_open_recommendation():
    engine = create_engine(require_disposable_postgres())
    workspace_id, account_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    slug=f"constraints-{workspace_id}",
                    name="Constraints",
                )
            )
            session.flush()
            session.add(
                Account(
                    id=account_id,
                    workspace_id=workspace_id,
                    display_name="Acme",
                    normalized_name="acme",
                )
            )

        with Session(engine) as session:
            session.add(
                Recommendation(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    rule_code="inbound_awaiting_response",
                    priority="high",
                    evidence_json=[],
                    state="open",
                    dedupe_key="same",
                    observed_at=now,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()

        with Session(engine) as session:
            session.add_all(
                [
                    Recommendation(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        rule_code="inbound_awaiting_response",
                        priority="high",
                        evidence_json=["activity:first"],
                        state="open",
                        dedupe_key="same",
                        observed_at=now,
                    ),
                    Recommendation(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        rule_code="inbound_awaiting_response",
                        priority="high",
                        evidence_json=["activity:second"],
                        state="open",
                        dedupe_key="same",
                        observed_at=now,
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        cleanup_workspace(engine, workspace_id)
        engine.dispose()
