from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.routers.accounts import (
    AccountRequestContext,
    get_account_request_context,
)
from dashboard.app.security import CRMPrincipal
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    Lead,
    Proposal,
    ProposalFollowup,
    ProposalItem,
    ProposalVersion,
    Workspace,
)
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def account_api_fixture():
    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    account_id = uuid4()
    empty_account_id = uuid4()
    foreign_account_id = uuid4()

    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=workspace_id,
                    slug=f"accounts-{workspace_id}",
                    name="Accounts Fixture",
                ),
                Workspace(
                    id=other_workspace_id,
                    slug=f"other-{other_workspace_id}",
                    name="Other Fixture",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Account(
                    id=account_id,
                    workspace_id=workspace_id,
                    display_name="Acme Transport",
                    normalized_name="acme transport",
                    lifecycle_stage="meeting",
                    highest_stage_rank=50,
                    sector="Logistics",
                ),
                Account(
                    id=empty_account_id,
                    workspace_id=workspace_id,
                    display_name="Empty Account",
                    normalized_name="empty account",
                    lifecycle_stage="meeting",
                    highest_stage_rank=40,
                ),
                Account(
                    id=foreign_account_id,
                    workspace_id=other_workspace_id,
                    display_name="Foreign Account",
                    normalized_name="foreign account",
                    lifecycle_stage="customer",
                    highest_stage_rank=90,
                ),
            ]
        )
        session.flush()
        contact_id = uuid4()
        lead_id = uuid4()
        session.add(
            Contact(
                id=contact_id,
                workspace_id=workspace_id,
                account_id=account_id,
                full_name="Ana Silva",
                primary_email="ana@example.test",
                is_primary=True,
            )
        )
        session.flush()
        session.add(
            Lead(
                id=lead_id,
                workspace_id=workspace_id,
                account_id=account_id,
                contact_id=contact_id,
                stage="meeting_held",
                highest_stage_rank=50,
            )
        )
        session.flush()
        occurred = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
        session.add_all(
            [
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    contact_id=contact_id,
                    activity_type="meeting",
                    occurred_at=occurred,
                    title="Discovery meeting",
                    summary="Private meeting notes must not be returned",
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    activity_type="email_sent",
                    occurred_at=occurred,
                    title="Email sent",
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    activity_type="email_received",
                    occurred_at=occurred,
                    title="Email received",
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    activity_type="proposal",
                    occurred_at=occurred,
                    title="Proposal evidence",
                ),
                Activity(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    lead_id=lead_id,
                    activity_type="task",
                    occurred_at=occurred,
                    title="Prepare follow-up",
                    summary="Private task detail must not be returned",
                ),
            ]
        )

    def override_context():
        with Session(engine) as session:
            yield AccountRequestContext(
                principal=CRMPrincipal(
                    workspace_id=workspace_id, subject="fixture-user"
                ),
                session=session,
            )

    dashboard_main.app.dependency_overrides[get_account_request_context] = (
        override_context
    )
    try:
        yield (
            TestClient(dashboard_main.app),
            {
                "account_id": account_id,
                "workspace_id": workspace_id,
                "empty_account_id": empty_account_id,
                "foreign_account_id": foreign_account_id,
                "engine": engine,
            },
        )
    finally:
        dashboard_main.app.dependency_overrides.clear()
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, other_workspace_id)
        engine.dispose()


@pytest.fixture
def proposal_api_fixture():
    from dashboard.app.routers.proposals import (
        ProposalRequestContext,
        get_proposal_request_context,
    )

    database_url = require_disposable_postgres()
    engine = create_engine(database_url)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    account_id = uuid4()
    owner_id = uuid4()
    confirmed_id = uuid4()
    candidate_id = uuid4()
    missing_id = uuid4()
    won_id = uuid4()
    lost_id = uuid4()
    foreign_id = uuid4()
    sent_evidence_id = uuid4()
    document_evidence_id = uuid4()
    confirmer_id = uuid4()
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=workspace_id,
                    slug=f"proposals-{workspace_id}",
                    name="Proposals Fixture",
                ),
                Workspace(
                    id=other_workspace_id,
                    slug=f"proposals-other-{other_workspace_id}",
                    name="Other Proposals Fixture",
                ),
            ]
        )
        session.flush()
        account = Account(
            id=account_id,
            workspace_id=workspace_id,
            display_name="Acme Transport",
            normalized_name="acme transport",
            lifecycle_stage="proposal",
            highest_stage_rank=70,
            commercial_vertical="Logistics",
        )
        foreign_account = Account(
            workspace_id=other_workspace_id,
            display_name="Foreign Buyer",
            normalized_name="foreign buyer",
            lifecycle_stage="proposal",
            highest_stage_rank=70,
        )
        session.add_all([account, foreign_account])
        session.flush()

        confirmed = Proposal(
            id=confirmed_id,
            workspace_id=workspace_id,
            account_id=account_id,
            title="Confirmed implementation",
            status="sent",
            sent_at=now - timedelta(days=10),
            sent_evidence_id=sent_evidence_id,
            sent_verification_state="verified",
            currency="EUR",
            probability=Decimal("50.00"),
            probability_source="sales_approved",
            forecast_category="commit",
            next_action="Follow up",
            next_action_due_at=now + timedelta(days=1),
            owner_user_id=owner_id,
        )
        candidate = Proposal(
            id=candidate_id,
            workspace_id=workspace_id,
            account_id=account_id,
            title="Candidate value",
            status="sent",
            sent_at=now - timedelta(days=20),
            sent_verification_state="legacy_unverified",
            currency="EUR",
        )
        missing = Proposal(
            id=missing_id,
            workspace_id=workspace_id,
            account_id=account_id,
            title="Value unknown",
            status="draft",
            currency="EUR",
        )
        won = Proposal(
            id=won_id,
            workspace_id=workspace_id,
            account_id=account_id,
            title="Won international project",
            status="won",
            sent_at=now - timedelta(days=30),
            sent_verification_state="legacy_unverified",
            currency="USD",
            won_at=now - timedelta(days=2),
        )
        lost = Proposal(
            id=lost_id,
            workspace_id=workspace_id,
            account_id=account_id,
            title="Lost advisory project",
            status="lost",
            sent_at=now - timedelta(days=40),
            sent_verification_state="legacy_unverified",
            currency="GBP",
            lost_at=now - timedelta(days=3),
            lost_reason="Budget redirected",
        )
        foreign = Proposal(
            id=foreign_id,
            workspace_id=other_workspace_id,
            account_id=foreign_account.id,
            title="Foreign private proposal",
            status="draft",
            currency="EUR",
        )
        session.add_all([confirmed, candidate, missing, won, lost, foreign])
        session.flush()

        confirmed_version = ProposalVersion(
            proposal_id=confirmed_id,
            version_number=1,
            status="sent",
            sent_at=confirmed.sent_at,
            one_off_amount=Decimal("1000.00"),
            mrr_amount=Decimal("100.00"),
            source_document_evidence_id=document_evidence_id,
            confirmed_by=confirmer_id,
            confirmed_at=now - timedelta(days=9),
        )
        candidate_version = ProposalVersion(
            proposal_id=candidate_id,
            version_number=1,
            one_off_amount=Decimal("9999.00"),
            extraction_confidence=Decimal("0.7500"),
        )
        won_version = ProposalVersion(
            proposal_id=won_id,
            version_number=1,
            status="accepted",
            one_off_amount=Decimal("500.00"),
            arr_amount=Decimal("1200.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=confirmer_id,
            confirmed_at=now - timedelta(days=2),
        )
        lost_version = ProposalVersion(
            proposal_id=lost_id,
            version_number=1,
            status="sent",
            sent_at=lost.sent_at,
            one_off_amount=Decimal("250.00"),
            source_document_evidence_id=uuid4(),
            confirmed_by=confirmer_id,
            confirmed_at=now - timedelta(days=3),
        )
        session.add_all(
            [confirmed_version, candidate_version, won_version, lost_version]
        )
        session.flush()
        confirmed.selected_version_id = confirmed_version.id
        confirmed.value_state = "confirmed"
        candidate.selected_version_id = candidate_version.id
        candidate.value_state = "candidate"
        won.selected_version_id = won_version.id
        won.value_state = "confirmed"
        lost.selected_version_id = lost_version.id
        lost.value_state = "confirmed"
        session.add(
            ProposalItem(
                proposal_version_id=confirmed_version.id,
                description="Implementation",
                amount=Decimal("1000.00"),
                currency="EUR",
                is_selected=True,
            )
        )
        activities = [
            Activity(
                workspace_id=workspace_id,
                account_id=account_id,
                activity_type="email_sent",
                occurred_at=now - timedelta(days=5 - index),
                title="Proposal follow-up",
                summary="Private notes for buyer@example.test",
            )
            for index in range(2)
        ]
        session.add_all(activities)
        session.flush()
        session.add_all(
            [
                ProposalFollowup(
                    proposal_id=confirmed_id,
                    activity_id=activity.id,
                    sequence_number=index + 1,
                    occurred_at=activity.occurred_at,
                    channel="email",
                )
                for index, activity in enumerate(activities)
            ]
        )

    def override_context():
        with Session(engine) as session:
            yield ProposalRequestContext(
                principal=CRMPrincipal(
                    workspace_id=workspace_id, subject="fixture-user"
                ),
                session=session,
            )

    dashboard_main.app.dependency_overrides[get_proposal_request_context] = (
        override_context
    )
    try:
        yield (
            TestClient(dashboard_main.app),
            {
                "workspace_id": workspace_id,
                "other_workspace_id": other_workspace_id,
                "account_id": account_id,
                "owner_id": owner_id,
                "confirmed_id": confirmed_id,
                "candidate_id": candidate_id,
                "missing_id": missing_id,
                "won_id": won_id,
                "lost_id": lost_id,
                "foreign_id": foreign_id,
                "sent_evidence_id": sent_evidence_id,
                "document_evidence_id": document_evidence_id,
                "engine": engine,
            },
        )
    finally:
        dashboard_main.app.dependency_overrides.clear()
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, other_workspace_id)
        engine.dispose()
