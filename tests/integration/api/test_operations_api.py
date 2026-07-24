from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.routers.operations import (
    OperationsRequestContext,
    get_operations_request_context,
)
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import Workspace
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


def test_operations_routes_fail_closed_without_trusted_admin():
    client = TestClient(dashboard_main.app)

    for path in ("/operacoes", "/api/v1/operations/metrics"):
        response = client.get(path)
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}


def test_operations_routes_reject_authenticated_non_admin():
    workspace_id = uuid4()
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: (
        CRMPrincipal(workspace_id=workspace_id, subject="non-admin")
    )
    try:
        client = TestClient(dashboard_main.app)
        for path in ("/operacoes", "/api/v1/operations/metrics"):
            response = client.get(path)
            assert response.status_code == 403
            assert response.json() == {"detail": "Forbidden"}
    finally:
        dashboard_main.app.dependency_overrides.clear()


@pytest.fixture
def operations_api_fixture():
    engine = create_engine(require_disposable_postgres())
    workspace_id, foreign_workspace_id = uuid4(), uuid4()
    account_id, foreign_account_id = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Workspace(id=workspace_id, slug=f"ops-{workspace_id}", name="Ops"),
                Workspace(
                    id=foreign_workspace_id,
                    slug=f"ops-{foreign_workspace_id}",
                    name="Foreign private workspace",
                ),
            ]
        )
        session.flush()
        statements = """
                INSERT INTO accounts
                    (id, workspace_id, display_name, normalized_name,
                     lifecycle_stage, highest_stage_rank)
                VALUES
                    (:account_id, :workspace_id, 'Visible only as a count', 'visible',
                     'potential', 10),
                    (:foreign_account_id, :foreign_workspace_id, 'Private foreign buyer',
                     'private foreign buyer', 'customer', 90);
                INSERT INTO leads
                    (id, workspace_id, account_id, stage, highest_stage_rank)
                VALUES
                    (:lead_id, :workspace_id, :account_id, 'meeting_held', 50),
                    (:orphan_lead_id, :workspace_id, NULL, 'qualified', 30);
                INSERT INTO ingest_events
                    (id, workspace_id, source_system, source_scope, event_type,
                     schema_version, idempotency_key, occurred_at, received_at, payload,
                     payload_hash, processing_status)
                VALUES
                    (:event_id, :workspace_id, 'manual', 'ops-test', 'test.event', 1,
                     'ops-event', CURRENT_TIMESTAMP - interval '3 minutes',
                     CURRENT_TIMESTAMP - interval '2 minutes',
                     '{"private":"must-not-leak"}'::jsonb, repeat('a', 64), 'received'),
                    (:review_id, :workspace_id, 'manual', 'ops-test', 'test.review', 1,
                     'ops-review', CURRENT_TIMESTAMP - interval '2 minutes',
                     CURRENT_TIMESTAMP - interval '1 minute', '{}'::jsonb,
                     repeat('b', 64), 'review'),
                    (:dead_id, :workspace_id, 'manual', 'ops-test', 'test.dead', 1,
                     'ops-dead', CURRENT_TIMESTAMP - interval '1 minute',
                     CURRENT_TIMESTAMP - interval '30 seconds', '{}'::jsonb,
                     repeat('c', 64), 'dead_letter'),
                    (:foreign_event_id, :foreign_workspace_id, 'manual', 'private-scope',
                     'private.raw.event', 1, 'foreign-event', CURRENT_TIMESTAMP - interval '1 day',
                     CURRENT_TIMESTAMP - interval '1 day',
                     '{"secret":"foreign-secret"}'::jsonb, repeat('d', 64), 'dead_letter');
                INSERT INTO sync_checkpoints
                    (id, workspace_id, connector, source_scope, stream, last_success_at)
                VALUES
                    (:checkpoint_id, :workspace_id, 'manual', 'ops-test', 'events',
                     CURRENT_TIMESTAMP - interval '5 minutes');
                INSERT INTO reconciliation_runs
                    (id, workspace_id, connector, source_scope, window_start_at,
                     window_end_at, started_at, finished_at, status, scanned_count,
                     created_count, updated_count, duplicate_count, conflict_count,
                     error_count, report)
                VALUES
                    (:reconciliation_id, :workspace_id, 'gmail', 'mailbox:ops-test',
                     CURRENT_TIMESTAMP - interval '1 day', CURRENT_TIMESTAMP,
                     CURRENT_TIMESTAMP - interval '6 minutes',
                     CURRENT_TIMESTAMP - interval '5 minutes', 'succeeded', 10, 2, 1, 4, 2, 1,
                     jsonb_build_object('conflict', 2, 'error', 1)),
                    (:foreign_reconciliation_id, :foreign_workspace_id, 'gmail',
                     'mailbox:private', CURRENT_TIMESTAMP - interval '1 day', CURRENT_TIMESTAMP,
                     CURRENT_TIMESTAMP - interval '6 minutes',
                     CURRENT_TIMESTAMP - interval '5 minutes', 'succeeded', 100, 0, 0, 0, 99, 1,
                     jsonb_build_object('conflict', 99, 'error', 1));
                INSERT INTO proposals
                    (id, workspace_id, account_id, title, currency, value_state)
                VALUES
                    (:proposal_id, :workspace_id, :account_id, 'Missing value', 'EUR', 'missing');
                INSERT INTO outbox_events
                    (id, workspace_id, command_id, semantic_hash, event_type,
                     aggregate_type, aggregate_id, payload, status, created_at)
                VALUES
                    (:outbox_id, :workspace_id, :command_id, repeat('e', 64),
                     'account.changed', 'account', :account_id, '{}', 'pending',
                     CURRENT_TIMESTAMP - interval '4 minutes');
                """
        parameters = {
            "workspace_id": workspace_id,
            "foreign_workspace_id": foreign_workspace_id,
            "account_id": account_id,
            "foreign_account_id": foreign_account_id,
            "lead_id": uuid4(),
            "orphan_lead_id": uuid4(),
            "event_id": uuid4(),
            "review_id": uuid4(),
            "dead_id": uuid4(),
            "foreign_event_id": uuid4(),
            "checkpoint_id": uuid4(),
            "reconciliation_id": uuid4(),
            "foreign_reconciliation_id": uuid4(),
            "proposal_id": uuid4(),
            "outbox_id": uuid4(),
            "command_id": uuid4(),
        }
        for statement in statements.split(";"):
            if statement.strip():
                session.execute(text(statement), parameters)

    def override():
        with Session(engine) as session:
            yield OperationsRequestContext(
                CRMPrincipal(
                    workspace_id=workspace_id, subject="trusted-admin", is_admin=True
                ),
                session,
            )

    dashboard_main.app.dependency_overrides[get_operations_request_context] = override
    try:
        yield TestClient(dashboard_main.app), foreign_workspace_id
    finally:
        dashboard_main.app.dependency_overrides.clear()
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, foreign_workspace_id)
        engine.dispose()


def test_operations_metrics_are_canonical_workspace_scoped_and_redacted(
    operations_api_fixture,
):
    client, foreign_workspace_id = operations_api_fixture
    response = client.get(
        "/api/v1/operations/metrics",
        params={"workspace_id": foreign_workspace_id},
        headers={"X-Workspace-ID": str(foreign_workspace_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["database"] == {"status": "ok"}
    assert 115 <= body["event_lag_seconds"] <= 135
    assert 295 <= body["checkpoint_age_seconds"] <= 315
    assert body["dead_letter_count"] == 1
    assert body["reconciliation_mismatch_count"] == 3
    assert body["missing_value_count"] == 1
    assert body["account_invariant_violation_count"] == 1
    assert 235 <= body["outbox_lag_seconds"] <= 255
    assert body["observed_at"].endswith("Z")
    assert "private" not in response.text.lower()
    assert "secret" not in response.text.lower()
    assert "payload" not in response.text.lower()


def test_operations_page_is_admin_only_and_contains_no_data_payloads(
    operations_api_fixture,
):
    client, _ = operations_api_fixture
    response = client.get("/operacoes")

    assert response.status_code == 200
    assert "CRM Operations" in response.text
    assert 'name="viewport"' in response.text
    assert "overflow-wrap: anywhere" in response.text
    assert "foreign" not in response.text.lower()
    assert "payload" not in response.text.lower()
