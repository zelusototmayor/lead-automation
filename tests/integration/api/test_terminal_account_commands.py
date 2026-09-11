"""Canonical create -> terminal regressions, using only disposable PostgreSQL."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import (
    Account,
    Activity,
    AuditEvent,
    Contact,
    Lead,
    OutboxEvent,
    Task,
)
from src.crm.services.agent_work_service import lead_is_suppressed
from tests.integration.api.test_lead_commands_api import (
    _headers,
    _payload,
    lead_command_api as lead_command_api,
)


@pytest.fixture
def created_lead_api(lead_command_api):
    client, engine, workspace_id, _, actor_id = lead_command_api
    principal = CRMPrincipal(
        workspace_id=workspace_id,
        actor_id=actor_id,
        subject="terminal-regression",
        permissions=frozenset({"crm:read", "crm:lead:create", "crm:lead-stage:write"}),
    )
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    command_id = uuid4()
    response = client.post(
        "/api/v1/commands/leads",
        json={
            "command_id": str(command_id),
            "company": "QA terminal regression",
            "contact_name": "QA contact without channels",
            "notes": "Preserve original human context",
        },
        headers=_headers(command_id),
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    lead_id = UUID(response.json()["lead_id"])
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        assert (lead.stage, lead.highest_stage_rank) == ("new", 10)
        assert lead.account_id is not None and lead.contact_id is not None
        account_id, contact_id = lead.account_id, lead.contact_id
    yield client, engine, workspace_id, lead_id, account_id, contact_id


def _transition(client, lead_id, target, version, *, reviewed=False, command_id=None):
    command_id = command_id or uuid4()
    return client.post(
        f"/api/v1/commands/leads/{lead_id}/transition-stage",
        json=_payload(
            command_id,
            target_stage=target,
            expected_version=version,
            reviewed_correction=reviewed,
        ),
        headers=_headers(command_id),
    )


@pytest.mark.parametrize("terminal", ["not_a_fit", "lost"])
def test_canonical_create_can_close_with_existing_account_and_no_channels(
    created_lead_api, terminal
):
    client, engine, workspace_id, lead_id, account_id, contact_id = created_lead_api
    command_id = uuid4()
    first = _transition(client, lead_id, terminal, 1, command_id=command_id)
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2
    replay = _transition(client, lead_id, terminal, 1, command_id=command_id)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json() | {"replayed": True}

    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        contact = session.get(Contact, contact_id)
        account = session.get(Account, account_id)
        assert (lead.stage, lead.highest_stage_rank, lead.version) == (terminal, 90, 2)
        assert (lead.account_id, lead.contact_id) == (account_id, contact_id)
        assert contact.account_id == account_id
        assert (contact.full_name, contact.primary_email, contact.phone) == (
            "QA contact without channels",
            None,
            None,
        )
        assert account.display_name == "QA terminal regression"
        assert account.highest_stage_rank == 90
        assert lead_is_suppressed(session, lead) is True
        activities = session.scalars(
            select(Activity).where(Activity.lead_id == lead_id)
        ).all()
        assert len(activities) == 2
        assert [
            (a.from_stage, a.to_stage)
            for a in activities
            if a.activity_type == "stage_change"
        ] == [("new", terminal)]
        assert [a.summary for a in activities if a.activity_type == "note"] == [
            "Preserve original human context"
        ]
        for model, expected in (
            (Account, 1),
            (Contact, 1),
            (Task, 0),
            (AuditEvent, 2),
            (OutboxEvent, 2),
        ):
            assert (
                session.scalar(
                    select(func.count(model.id)).where(
                        model.workspace_id == workspace_id
                    )
                )
                == expected
            )


@pytest.mark.parametrize("terminal", ["not_a_fit", "lost"])
@pytest.mark.parametrize(
    "path",
    [
        ("contacted",),
        ("qualified",),
        ("meeting_booked", "new"),
        ("negotiation", "qualified"),
    ],
)
def test_terminal_close_preserves_existing_account_after_early_or_backward_path(
    created_lead_api, terminal, path
):
    client, engine, _, lead_id, account_id, contact_id = created_lead_api
    version = 1
    ranks = {
        "new": 10,
        "contacted": 20,
        "qualified": 30,
        "meeting_booked": 40,
        "negotiation": 80,
    }
    highest = 10
    for target in path:
        response = _transition(client, lead_id, target, version)
        assert response.status_code == 200, response.text
        version = response.json()["version"]
        highest = max(highest, ranks[target])
        with Session(engine) as session:
            assert session.get(Lead, lead_id).highest_stage_rank == highest
    response = _transition(client, lead_id, terminal, version)
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        assert (lead.stage, lead.highest_stage_rank) == (terminal, 90)
        assert (lead.account_id, lead.contact_id) == (account_id, contact_id)
        assert session.get(Account, account_id).highest_stage_rank == 90


@pytest.mark.parametrize("terminal", ["not_a_fit", "lost"])
def test_terminal_correction_remains_explicit_and_keeps_suppression(
    created_lead_api, terminal
):
    client, engine, workspace_id, lead_id, account_id, contact_id = created_lead_api
    with Session(engine) as session, session.begin():
        session.get(Contact, contact_id).status = "inactive"
    closed = _transition(client, lead_id, terminal, 1)
    assert closed.status_code == 200, closed.text
    other_terminal = "lost" if terminal == "not_a_fit" else "not_a_fit"
    for target in ("new", "meeting_booked", other_terminal, terminal):
        rejected = _transition(client, lead_id, target, 2)
        assert rejected.status_code == 409, rejected.text
    stale = _transition(client, lead_id, "new", 1, reviewed=True)
    assert stale.status_code == 409
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        assert (lead.stage, lead.version) == (terminal, 2)
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.workspace_id == workspace_id
                )
            )
            == 2
        )
    corrected = _transition(client, lead_id, other_terminal, 2, reviewed=True)
    assert corrected.status_code == 200, corrected.text
    reopened = _transition(
        client, lead_id, "new", corrected.json()["version"], reviewed=True
    )
    assert reopened.status_code == 200, reopened.text
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        assert lead.highest_stage_rank == 90
        assert session.get(Account, account_id).highest_stage_rank == 90
        assert (lead.account_id, lead.contact_id) == (account_id, contact_id)
        assert session.get(Contact, contact_id).status == "inactive"
        assert lead_is_suppressed(session, lead) is True
    # Even a reviewed backward correction leaves terminal rank tainted; the
    # existing linkage fallback must keep a later close possible, not lower rank.
    reclosed = _transition(client, lead_id, terminal, reopened.json()["version"])
    assert reclosed.status_code == 200, reclosed.text


@pytest.mark.parametrize("terminal", ["not_a_fit", "lost"])
def test_pre_account_terminal_history_does_not_fabricate_account(
    lead_command_api, terminal
):
    client, engine, workspace_id, lead_id, _ = lead_command_api
    closed = _transition(client, lead_id, terminal, 1)
    assert closed.status_code == 200, closed.text
    reopened = _transition(client, lead_id, "new", 2, reviewed=True)
    assert reopened.status_code == 200, reopened.text
    reclosed = _transition(client, lead_id, terminal, reopened.json()["version"])
    assert reclosed.status_code == 200, reclosed.text
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        assert (lead.stage, lead.highest_stage_rank) == (terminal, 90)
        assert lead.account_id is None and lead.contact_id is None
        assert lead_is_suppressed(session, lead) is True
        for model in (Account, Contact, Task):
            assert (
                session.scalar(
                    select(func.count(model.id)).where(
                        model.workspace_id == workspace_id
                    )
                )
                == 0
            )
