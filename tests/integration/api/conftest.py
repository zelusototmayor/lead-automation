from __future__ import annotations

from datetime import UTC, datetime
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
from src.crm.persistence.models import Account, Activity, Contact, Lead, Workspace
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
