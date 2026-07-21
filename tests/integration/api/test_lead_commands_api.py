from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_settings
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers import accounts as accounts_router
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Account,
    Activity,
    AuditEvent,
    Contact,
    Lead,
    OutboxEvent,
    Workspace,
)
from tests.migration._postgres import cleanup_workspace, require_disposable_postgres


@pytest.fixture
def lead_command_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, lead_id, actor_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"lead-command-{workspace_id}",
                name="Lead command API",
            )
        )
        session.flush()
        session.add(Lead(id=lead_id, workspace_id=workspace_id))

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
    monkeypatch.setattr(accounts_router, "_account_engine", lambda: engine)

    principal = CRMPrincipal(
        workspace_id=workspace_id,
        actor_id=actor_id,
        subject="command-tester",
        permissions=frozenset({"crm:read", "crm:lead-stage:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    try:
        yield TestClient(dashboard_main.app), engine, workspace_id, lead_id, actor_id
    finally:
        dashboard_main.app.dependency_overrides.clear()
        get_settings.cache_clear()
        get_feature_flags.cache_clear()
        cleanup_workspace(engine, workspace_id)
        engine.dispose()


def _headers(command_id) -> dict[str, str]:
    return {
        "Origin": "http://localhost:8000",
        "X-CSRF-Token": "csrf-test-token",
        "Idempotency-Key": str(command_id),
    }


def _payload(command_id, **changes):
    payload = {
        "command_id": str(command_id),
        "target_stage": "contacted",
        "expected_version": 1,
        "reviewed_correction": False,
    }
    payload.update(changes)
    return payload


def test_stage_command_is_atomic_audited_and_idempotent(lead_command_api):
    client, engine, workspace_id, lead_id, actor_id = lead_command_api
    command_id = uuid4()

    first = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id),
        headers=_headers(command_id),
    )
    replay = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id),
        headers=_headers(command_id),
    )

    assert first.status_code == replay.status_code == 200
    assert first.json() == {
        "command_id": str(command_id),
        "lead_id": str(lead_id),
        "version": 2,
        "replayed": False,
    }
    assert replay.json() == first.json() | {"replayed": True}
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        )
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id)
        )
        activity = session.scalar(
            select(Activity).where(Activity.workspace_id == workspace_id)
        )
        assert (lead.stage, lead.version) == ("contacted", 2)
        assert activity.activity_type == "stage_change"
        assert activity.lead_id == lead_id
        assert activity.title == "Stage changed"
        assert activity.summary is None
        assert audit.actor_id == actor_id
        assert audit.workspace_id == outbox.workspace_id == workspace_id
        assert audit.command_id == outbox.command_id == command_id
        assert (
            session.scalar(
                select(func.count(Activity.id)).where(
                    Activity.workspace_id == workspace_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.workspace_id == workspace_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.workspace_id == workspace_id
                )
            )
            == 1
        )


def test_stage_command_promotes_pre_account_lead_from_exact_identity(lead_command_api):
    client, engine, workspace_id, lead_id, _ = lead_command_api
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        lead.company_name = "Early Company"
        lead.contact_name = "Early Contact"
        lead.contact_email = "early@example.test"
        lead.contact_phone = "+351****0000"
        lead.city = "Porto"
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id, target_stage="meeting_booked", expected_version=2),
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        account = session.get(Account, lead.account_id)
        contact = session.get(Contact, lead.contact_id)
        activity = session.scalar(
            select(Activity).where(
                Activity.workspace_id == workspace_id,
                Activity.lead_id == lead_id,
            )
        )
        assert lead.stage == "meeting_booked"
        assert (
            lead.company_name,
            lead.contact_name,
            lead.contact_email,
            lead.contact_phone,
            lead.city,
        ) == (None, None, None, None, None)
        assert account.display_name == "Early Company"
        assert account.city == "Porto"
        assert account.highest_stage_rank == 40
        assert contact.account_id == account.id
        assert contact.full_name == "Early Contact"
        assert str(contact.primary_email) == "early@example.test"
        assert contact.phone == "+351****0000"
        assert activity.account_id == account.id
        assert (
            session.scalar(
                select(func.count(Account.id)).where(
                    Account.workspace_id == workspace_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(Contact.id)).where(
                    Contact.workspace_id == workspace_id
                )
            )
            == 1
        )


def test_stage_command_links_contact_when_account_already_exists(lead_command_api):
    client, engine, workspace_id, lead_id, _ = lead_command_api
    with Session(engine) as session, session.begin():
        account = Account(
            workspace_id=workspace_id,
            display_name="Existing Company",
            normalized_name="existing company",
            city="Porto",
        )
        session.add(account)
        session.flush()
        lead = session.get(Lead, lead_id)
        lead.account_id = account.id
        lead.company_name = "Existing Company"
        lead.contact_name = "Existing Contact"
        lead.contact_email = "existing-contact@example.test"
        lead.contact_phone = "+351000000444"
        lead.city = "Porto"
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id, target_stage="meeting_booked", expected_version=2),
        headers=_headers(command_id),
    )

    assert response.status_code == 200
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        contact = session.get(Contact, lead.contact_id)
        assert contact is not None
        assert contact.account_id == lead.account_id
        assert contact.full_name == "Existing Contact"
        assert str(contact.primary_email) == "existing-contact@example.test"
        assert contact.phone == "+351000000444"
        assert (
            lead.company_name,
            lead.contact_name,
            lead.contact_email,
            lead.contact_phone,
            lead.city,
        ) == (None, None, None, None, None)
        assert (
            session.scalar(
                select(func.count(Contact.id)).where(
                    Contact.workspace_id == workspace_id
                )
            )
            == 1
        )


def test_stage_command_rejects_cross_field_identity_conflict_atomically(
    lead_command_api,
):
    client, engine, workspace_id, lead_id, _ = lead_command_api
    with Session(engine) as session, session.begin():
        account = Account(
            workspace_id=workspace_id,
            display_name="Canonical Company",
            normalized_name="canonical company",
            city="Lisboa",
        )
        session.add(account)
        session.flush()
        session.add(
            Contact(
                workspace_id=workspace_id,
                account_id=account.id,
                full_name="Canonical Contact",
                primary_email="shared@example.test",
                phone="+351****1111",
                is_primary=True,
            )
        )
        lead = session.get(Lead, lead_id)
        lead.company_name = "Different Company"
        lead.contact_name = "Canonical Contact"
        lead.contact_email = "shared@example.test"
        lead.contact_phone = "+351****1111"
        lead.city = "Lisboa"
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id, target_stage="meeting_booked", expected_version=2),
        headers=_headers(command_id),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        assert lead.account_id is None
        assert lead.contact_id is None
        assert lead.stage == "new"
        assert (
            session.scalar(
                select(func.count(Account.id)).where(
                    Account.workspace_id == workspace_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(Contact.id)).where(
                    Contact.workspace_id == workspace_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(Activity.id)).where(
                    Activity.workspace_id == workspace_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.workspace_id == workspace_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.workspace_id == workspace_id
                )
            )
            == 0
        )


def test_stage_command_requires_matching_idempotency_key(lead_command_api):
    client, engine, _, lead_id, _ = lead_command_api
    command_id = uuid4()

    response = client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(command_id),
        headers={
            "Origin": "http://localhost:8000",
            "X-CSRF-Token": "csrf-test-token",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid command"}
    with Session(engine) as session:
        assert session.get(Lead, lead_id).version == 1
        assert session.scalar(select(func.count(AuditEvent.id))) == 0
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0


def test_stage_command_conflicts_are_generic_and_do_not_mutate(lead_command_api):
    client, engine, _, lead_id, _ = lead_command_api
    command_id = uuid4()
    assert (
        client.put(
            f"/api/v1/leads/{lead_id}/stage",
            json=_payload(command_id),
            headers=_headers(command_id),
        ).status_code
        == 200
    )

    conflict = client.put(
        f"/api/v1/leads/{lead_id}/stage",
        json=_payload(command_id, target_stage="replied"),
        headers=_headers(command_id),
    )
    missing = client.put(
        f"/api/v1/leads/{uuid4()}/stage",
        json=_payload(uuid4(), expected_version=1),
        headers=_headers(command_id),
    )

    assert conflict.status_code == missing.status_code == 409
    assert conflict.json() == missing.json() == {"detail": "Command conflict"}
    with Session(engine) as session:
        assert session.get(Lead, lead_id).stage == "contacted"
        assert session.scalar(select(func.count(AuditEvent.id))) == 1
        assert session.scalar(select(func.count(OutboxEvent.id))) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://localhost:8000"},
        {"X-CSRF-Token": "csrf-test-token"},
        {
            "Origin": "https://attacker.invalid",
            "X-CSRF-Token": "csrf-test-token",
        },
    ],
)
def test_stage_command_rejects_missing_or_invalid_csrf_origin_before_database(
    monkeypatch, headers
):
    monkeypatch.setenv("CRM_CSRF_TOKEN", "csrf-test-token")
    monkeypatch.setenv("CRM_ALLOWED_WRITE_ORIGINS", "http://localhost:8000")
    monkeypatch.setenv("CRM_ENV", "test")
    get_settings.cache_clear()
    principal = CRMPrincipal(
        workspace_id=uuid4(),
        actor_id=uuid4(),
        subject="command-tester",
        permissions=frozenset({"crm:read", "crm:lead-stage:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    monkeypatch.setattr(
        accounts_router,
        "_account_engine",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )
    try:
        response = TestClient(dashboard_main.app).put(
            f"/api/v1/leads/{uuid4()}/stage",
            json=_payload(uuid4()),
            headers=headers,
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}
    finally:
        dashboard_main.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_stage_command_requires_actor_and_exact_permission(lead_command_api):
    client, _, workspace_id, lead_id, _ = lead_command_api
    for principal in (
        CRMPrincipal(workspace_id=workspace_id, subject="read-only"),
        CRMPrincipal(
            workspace_id=workspace_id,
            actor_id=uuid4(),
            subject="wrong-permission",
            permissions=frozenset({"crm:read", "crm:lead:edit"}),
        ),
    ):
        dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: (
            principal
        )
        command_id = uuid4()
        response = client.put(
            f"/api/v1/leads/{lead_id}/stage",
            json=_payload(command_id),
            headers=_headers(command_id),
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}


def test_stage_command_replay_does_not_inflate_time_in_stage_analytics(
    lead_command_api,
):
    client, engine, workspace_id, lead_id, _ = lead_command_api
    first_id, second_id = uuid4(), uuid4()

    first_payload = _payload(first_id)
    second_payload = _payload(
        second_id,
        target_stage="qualified",
        expected_version=2,
    )
    for command_id, payload in (
        (first_id, first_payload),
        (first_id, first_payload),
        (second_id, second_payload),
        (second_id, second_payload),
    ):
        assert (
            client.post(
                f"/api/v1/commands/leads/{lead_id}/transition-stage",
                json=payload,
                headers=_headers(command_id),
            ).status_code
            == 200
        )

    analytics = client.get("/api/v1/pipeline/analytics?days=1")

    assert analytics.status_code == 200
    assert analytics.json()["time_in_stage"]["coverage"] == {
        "structured_transitions": 2,
        "legacy_transitions": 0,
        "usable_intervals": 1,
        "uncovered_transitions": 1,
    }
    assert analytics.json()["time_in_stage"]["stages"][0]["completed_intervals"] == 1
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count(Activity.id)).where(
                    Activity.workspace_id == workspace_id
                )
            )
            == 2
        )
