"""Disposable PostgreSQL/FastAPI cadence contract tests; never live evidence."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4
import pytest

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from src.crm.persistence.models import Activity, AuditEvent, OutboxEvent, Lead
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers


def details(**changes):
    return {
        "schema_version": 1, "attempted": True,
        "answer_kind": "human_counterparty", "useful": None,
        "decision_maker": False, "interlocutor_role": "reception",
        "repeat_reason": None,
    } | changes


def test_call_dimensions_persist_and_replay_without_inference(lead_operations_api):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    command = uuid4()
    payload = {
        "command_id": str(command), "expected_version": 1,
        "outcome_code": "connected", "call_details": details(),
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }
    url = f"/api/v1/commands/leads/{lead_id}/log-call"
    first = client.post(url, json=payload, headers=_headers(command))
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["call_details"] == details()
    assert result["activity_id"]
    replay = client.post(url, json=payload, headers=_headers(command))
    assert replay.json() == result | {"replayed": True}
    changed = client.post(url, json=payload | {"call_details": details(useful=True)}, headers=_headers(command))
    assert changed.status_code == 409
    with Session(engine) as session:
        activity = session.scalars(select(Activity).where(Activity.workspace_id == workspace_id, Activity.activity_type == "call")).one()
        assert str(activity.id) == result["activity_id"]
        assert activity.call_details == details()
        outbox = session.scalars(select(OutboxEvent).where(OutboxEvent.workspace_id == workspace_id, OutboxEvent.command_id == command)).one()
        audit = session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id, AuditEvent.command_id == command)).one()
        assert outbox.payload["call_details"] == audit.details["call_details"] == details()


def test_call_timeline_preserves_nullable_dimensions_and_derived_answer(lead_operations_api, monkeypatch):
    from dashboard.app.routers import accounts
    client, engine, _, lead_id, _ = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    for index, value in enumerate([None, details(), details(answer_kind="unknown"), details(answer_kind="ivr")]):
        command = uuid4()
        payload = {"command_id": str(command), "expected_version": index + 1, "outcome_code": "connected"}
        if value is not None:
            payload["call_details"] = value
        response = client.post(f"/api/v1/commands/leads/{lead_id}/log-call", json=payload, headers=_headers(command))
        assert response.status_code == 200, response.text
    timeline = client.get(f"/api/v1/leads/{lead_id}/timeline").json()["items"]
    assert len(timeline) == 4
    assert [row["answered"] for row in timeline] == [False, None, True, None]
    assert [row["call_details"] for row in timeline] == [details(answer_kind="ivr"), details(answer_kind="unknown"), details(), None]


@pytest.mark.parametrize("invalid", [[], {}, {"schema_version": True}, {"schema_version": "1"}, {"schema_version": 2}])
def test_db_rejects_nonobject_or_unversioned_call_details(lead_operations_api, invalid):
    _, engine, workspace_id, lead_id, _ = lead_operations_api
    with Session(engine) as session:
        session.add(Activity(workspace_id=workspace_id, lead_id=lead_id, account_id=session.get(Lead, lead_id).account_id, activity_type="call", title="Test call", occurred_at=datetime.now(UTC), call_details=invalid))
        with pytest.raises(IntegrityError) as error:
            session.flush()
        assert error.value.orig.diag.constraint_name == "ck_activities_call_details_v1"


def test_incomplete_persisted_dimensions_do_not_become_negative_answer(lead_operations_api, monkeypatch):
    from dashboard.app.routers import accounts
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    with Session(engine) as session, session.begin():
        session.add(Activity(workspace_id=workspace_id, lead_id=lead_id, account_id=session.get(Lead, lead_id).account_id, activity_type="call", title="Incomplete retained fact", occurred_at=datetime.now(UTC), call_details={"schema_version": 1}))
    item = client.get(f"/api/v1/leads/{lead_id}/timeline").json()["items"][0]
    assert item["answered"] is None


@pytest.mark.parametrize("changes", [
    {"attempted": 1}, {"schema_version": True}, {"useful": "true"},
    {"answer_kind": "ivr", "useful": True},
    {"answer_kind": "voicemail", "decision_maker": True, "interlocutor_role": "decision_maker"},
    {"decision_maker": True}, {"repeat_reason": " "}, {"repeat_reason": "x" * 501},
    {"repeat_reason": 23}, {"hidden": True},
])
def test_call_dimensions_reject_contradictory_or_coerced_facts(lead_operations_api, changes):
    client, _, _, lead_id, _ = lead_operations_api
    command = uuid4()
    result = client.post(f"/api/v1/commands/leads/{lead_id}/log-call", json={
        "command_id": str(command), "expected_version": 1,
        "outcome_code": "connected", "call_details": details(**changes),
    }, headers=_headers(command))
    assert result.status_code == 422, result.text


@pytest.mark.parametrize("outcome", ["no_answer", "voicemail", "wrong_number"])
def test_negative_legacy_outcome_cannot_claim_human_answer(lead_operations_api, outcome):
    client, _, _, lead_id, _ = lead_operations_api
    command = uuid4()
    result = client.post(f"/api/v1/commands/leads/{lead_id}/log-call", json={
        "command_id": str(command), "expected_version": 1,
        "outcome_code": outcome, "call_details": details(),
    }, headers=_headers(command))
    assert result.status_code == 422, result.text
