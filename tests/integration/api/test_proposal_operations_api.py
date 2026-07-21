from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers import proposal_commands as proposal_commands_router
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Account,
    Activity,
    AuditEvent,
    OutboxEvent,
    Proposal,
    Workspace,
)
from src.crm.services import proposal_operation_service
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def proposal_operations_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, other_workspace_id = uuid4(), uuid4()
    account_id, proposal_id, foreign_account_id, foreign_proposal_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    actor_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=workspace_id,
                    slug=f"proposal-operations-{workspace_id}",
                    name="Proposal operations API",
                ),
                Workspace(
                    id=other_workspace_id,
                    slug=f"proposal-operations-other-{other_workspace_id}",
                    name="Other proposal operations API",
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
                    normalized_name=f"acme transport {workspace_id}",
                ),
                Account(
                    id=foreign_account_id,
                    workspace_id=other_workspace_id,
                    display_name="Foreign Buyer",
                    normalized_name=f"foreign buyer {other_workspace_id}",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Proposal(
                    id=proposal_id,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    title="Implementation",
                    status="sent",
                    sent_at=datetime.now(UTC) - timedelta(days=3),
                    sent_verification_state="legacy_unverified",
                    currency="EUR",
                    probability=Decimal("40.00"),
                    probability_source="sales_approved",
                    forecast_category="pipeline",
                    next_action="Initial follow-up",
                    next_action_due_at=datetime.now(UTC) + timedelta(days=1),
                ),
                Proposal(
                    id=foreign_proposal_id,
                    workspace_id=other_workspace_id,
                    account_id=foreign_account_id,
                    title="Foreign proposal",
                    status="draft",
                    currency="EUR",
                ),
            ]
        )

    for name, value in {
        "CRM_DB_ENABLED": "true",
        "CRM_ACCOUNTS_READ_MODEL": "postgres",
        "CRM_PROPOSALS_READ_MODEL": "postgres",
        "CRM_COMMAND_WRITER": "postgres",
        "CRM_SHEETS_PROJECTION_ENABLED": "false",
        "CRM_AGENT_EVENTS_ENABLED": "false",
        "CRM_CSRF_TOKEN": "csrf-test-token",
        "CRM_ALLOWED_WRITE_ORIGINS": "http://localhost:8000",
        "CRM_ENV": "test",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    get_feature_flags.cache_clear()
    monkeypatch.setattr(
        proposal_commands_router, "_proposal_operation_engine", lambda: engine
    )
    principal = CRMPrincipal(
        workspace_id=workspace_id,
        actor_id=actor_id,
        subject="proposal-operations-tester",
        permissions=frozenset({"crm:read", "crm:proposal:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    try:
        yield (
            TestClient(dashboard_main.app),
            engine,
            workspace_id,
            proposal_id,
            foreign_proposal_id,
            actor_id,
        )
    finally:
        dashboard_main.app.dependency_overrides.clear()
        get_settings.cache_clear()
        get_feature_flags.cache_clear()
        cleanup_workspace(engine, workspace_id)
        cleanup_workspace(engine, other_workspace_id)
        engine.dispose()


def _headers(command_id) -> dict[str, str]:
    return {
        "Origin": "http://localhost:8000",
        "X-CSRF-Token": "csrf-test-token",
        "Idempotency-Key": str(command_id),
    }


def _payload(command_id, *, expected_version: int = 1) -> dict[str, object]:
    return {
        "command_id": str(command_id),
        "expected_version": expected_version,
        "status": "negotiation",
        "probability": "70.00",
        "forecast_category": "commit",
        "next_action": "Review final scope",
        "next_action_due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "lost_reason": None,
    }


def _count(session: Session, model, workspace_id) -> int:
    return int(
        session.scalar(
            select(func.count(model.id)).where(model.workspace_id == workspace_id)
        )
        or 0
    )


def test_update_proposal_pipeline_is_atomic_and_audited(proposal_operations_api):
    client, engine, workspace_id, proposal_id, _, actor_id = proposal_operations_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=_payload(command_id),
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    assert response.json() == {
        "command_id": str(command_id),
        "proposal_id": str(proposal_id),
        "version": 2,
        "replayed": False,
    }
    with Session(engine) as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal.status == "negotiation"
        assert proposal.probability == Decimal("70.00")
        assert proposal.probability_source == "manual"
        assert proposal.forecast_category == "commit"
        assert proposal.next_action == "Review final scope"
        assert proposal.next_action_due_at is not None
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        )
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        assert activity.account_id == proposal.account_id
        assert activity.actor_id == actor_id
        assert activity.title == "Proposal pipeline updated"
        assert audit.action == outbox.event_type == "proposal.pipeline_updated"
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_update_proposal_pipeline_rolls_back_when_outbox_enqueue_fails(
    proposal_operations_api, monkeypatch
):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("forced outbox failure")

    monkeypatch.setattr(
        proposal_operation_service, "enqueue_outbox_event", fail_enqueue
    )

    response = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=_payload(command_id),
        headers=_headers(command_id),
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "forced outbox failure" not in response.text
    assert response.headers["cache-control"] == "no-store"

    with Session(engine) as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal.version == 1
        assert proposal.status == "sent"
        assert proposal.probability == Decimal("40.00")
        assert proposal.next_action == "Initial follow-up"
        assert _count(session, Activity, workspace_id) == 0
        assert _count(session, AuditEvent, workspace_id) == 0
        assert _count(session, OutboxEvent, workspace_id) == 0


def test_update_proposal_replays_same_command_without_duplicates(
    proposal_operations_api,
):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()
    payload = _payload(command_id)

    first = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=payload,
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=payload,
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["version"] == 2
    with Session(engine) as session:
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_update_proposal_replays_semantically_equivalent_probability(
    proposal_operations_api,
):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()
    first_payload = _payload(command_id)
    first_payload["probability"] = "70"
    replay_payload = dict(first_payload)
    replay_payload["probability"] = "70.00"

    first = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=first_payload,
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=replay_payload,
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json()["replayed"] is True
    with Session(engine) as session:
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_update_proposal_rejects_divergent_replay_without_new_writes(
    proposal_operations_api,
):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()
    first_payload = _payload(command_id)
    divergent_payload = dict(first_payload)
    divergent_payload["next_action"] = "A different action"

    first = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=first_payload,
        headers=_headers(command_id),
    )
    divergent = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=divergent_payload,
        headers=_headers(command_id),
    )

    assert first.status_code == 200
    assert divergent.status_code == 409
    assert divergent.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal.version == 2
        assert proposal.next_action == first_payload["next_action"]
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_update_proposal_persists_lost_outcome_and_reason(proposal_operations_api):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()
    payload = _payload(command_id)
    payload.update(status="lost", lost_reason="Budget allocated elsewhere")

    response = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=payload,
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    with Session(engine) as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal.status == "lost"
        assert proposal.lost_reason == "Budget allocated elsewhere"
        assert proposal.lost_at is not None
        assert proposal.won_at is None
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1


def test_update_proposal_rejects_won_until_official_proof_policy_exists(
    proposal_operations_api,
):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()
    payload = _payload(command_id)
    payload["status"] = "won"

    response = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=payload,
        headers=_headers(command_id),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal.version == 1
        assert proposal.status == "sent"
        assert _count(session, Activity, workspace_id) == 0
        assert _count(session, AuditEvent, workspace_id) == 0
        assert _count(session, OutboxEvent, workspace_id) == 0


@pytest.mark.parametrize(
    ("mutate", "expected_status"),
    [
        (lambda payload: payload.update(expected_version=99), 409),
        (lambda payload: payload.update(probability="101.00"), 422),
        (
            lambda payload: payload.update(status="lost", lost_reason=None),
            422,
        ),
        (
            lambda payload: payload.update(
                next_action=None, next_action_due_at=datetime.now(UTC).isoformat()
            ),
            422,
        ),
    ],
)
def test_update_proposal_rejects_invalid_or_stale_commands(
    proposal_operations_api, mutate, expected_status
):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()
    payload = _payload(command_id)
    mutate(payload)

    response = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=payload,
        headers=_headers(command_id),
    )

    assert response.status_code == expected_status
    with Session(engine) as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal.version == 1
        assert proposal.status == "sent"
        assert _count(session, Activity, workspace_id) == 0
        assert _count(session, AuditEvent, workspace_id) == 0
        assert _count(session, OutboxEvent, workspace_id) == 0


def test_update_proposal_hides_foreign_workspace_and_rejects_untrusted_origin(
    proposal_operations_api,
):
    client, engine, workspace_id, _, foreign_proposal_id, _ = proposal_operations_api
    command_id = uuid4()

    foreign = client.post(
        f"/api/v1/commands/proposals/{foreign_proposal_id}/update-pipeline",
        json=_payload(command_id),
        headers=_headers(command_id),
    )
    bad_origin_headers = _headers(uuid4())
    bad_origin_headers["Origin"] = "https://evil.example"
    bad_origin = client.post(
        f"/api/v1/commands/proposals/{uuid4()}/update-pipeline",
        json=_payload(UUID(bad_origin_headers["Idempotency-Key"])),
        headers=bad_origin_headers,
    )

    assert foreign.status_code == 409
    assert foreign.json() == {"detail": "Command conflict"}
    assert bad_origin.status_code == 403
    with Session(engine) as session:
        assert _count(session, Activity, workspace_id) == 0
        assert _count(session, AuditEvent, workspace_id) == 0
        assert _count(session, OutboxEvent, workspace_id) == 0


def test_update_proposal_authenticates_before_database_access(monkeypatch):
    for name, value in {
        "CRM_DB_ENABLED": "true",
        "CRM_PROPOSALS_READ_MODEL": "postgres",
        "CRM_COMMAND_WRITER": "postgres",
        "CRM_CSRF_TOKEN": "csrf-test-token",
        "CRM_ALLOWED_WRITE_ORIGINS": "http://localhost:8000",
        "CRM_ENV": "test",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    get_feature_flags.cache_clear()
    dashboard_main.app.dependency_overrides.clear()
    database_touched = False

    def forbidden_engine_access():
        nonlocal database_touched
        database_touched = True
        raise AssertionError("database accessed before authentication")

    monkeypatch.setattr(
        proposal_commands_router,
        "_proposal_operation_engine",
        forbidden_engine_access,
    )
    command_id = uuid4()

    response = TestClient(dashboard_main.app).post(
        f"/api/v1/commands/proposals/{uuid4()}/update-pipeline",
        json=_payload(command_id),
        headers=_headers(command_id),
    )

    assert response.status_code in {401, 403}
    assert database_touched is False
    get_settings.cache_clear()
    get_feature_flags.cache_clear()


def test_proposal_replay_by_a_different_actor_is_a_generic_conflict(
    proposal_operations_api,
):
    client, engine, workspace_id, proposal_id, _, _ = proposal_operations_api
    command_id = uuid4()
    payload = _payload(command_id)
    assert (
        client.post(
            f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
            json=payload,
            headers=_headers(command_id),
        ).status_code
        == 200
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: CRMPrincipal(
        workspace_id=workspace_id,
        actor_id=uuid4(),
        subject="different-proposal-actor",
        permissions=frozenset({"crm:read", "crm:proposal:write"}),
    )

    replay = client.post(
        f"/api/v1/commands/proposals/{proposal_id}/update-pipeline",
        json=payload,
        headers=_headers(command_id),
    )

    assert replay.status_code == 409
    assert replay.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        assert session.get(Proposal, proposal_id).version == 2
        assert _count(session, Activity, workspace_id) == 1
        assert _count(session, AuditEvent, workspace_id) == 1
        assert _count(session, OutboxEvent, workspace_id) == 1
