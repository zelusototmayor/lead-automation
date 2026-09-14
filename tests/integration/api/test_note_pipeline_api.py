"""Note-only tracer through the real HTTP writer and disposable PostgreSQL."""
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.crm.persistence.models import Activity, AgentWork
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers


def test_note_only_call_is_unknown_and_replay_safe(lead_operations_api):
    client, engine, workspace, lead, _ = lead_operations_api
    command = uuid4()
    body = {"command_id": str(command), "expected_version": 1,
            "summary": "Duas tentativas narradas; receção encaminhou. Confirmar contacto."}
    url = f"/api/v1/commands/leads/{lead}/log-call"
    response = client.post(url, json=body, headers=_headers(command))
    assert response.status_code == 200, response.text
    assert client.post(url, json=body, headers=_headers(command)).json()["replayed"] is True
    with Session(engine) as session:
        rows = list(session.scalars(select(Activity).where(Activity.workspace_id == workspace)))
        assert len(rows) == 1
        assert rows[0].outcome_code is None
        assert rows[0].summary == body["summary"]
        assert rows[0].call_details is None
        work = list(session.scalars(select(AgentWork).where(AgentWork.workspace_id == workspace)))
        assert len(work) == 1
        assert work[0].kind == "call_followup"
        assert work[0].payload["activity_id"] == str(rows[0].id)
        assert len(work[0].payload["source_digest"]) == 64


def test_standalone_note_enqueues_exact_source_once(lead_operations_api):
    client, engine, workspace, lead, _ = lead_operations_api
    command = uuid4()
    body = {"command_id": str(command), "expected_version": 1,
            "summary": "Preparar intro para hoje; confirmar endereço literal."}
    url = f"/api/v1/commands/leads/{lead}/add-note"
    response = client.post(url, json=body, headers=_headers(command))
    assert response.status_code == 200, response.text
    assert client.post(url, json=body, headers=_headers(command)).json()["replayed"] is True
    with Session(engine) as session:
        source = session.scalar(select(Activity).where(Activity.workspace_id == workspace))
        work = list(session.scalars(select(AgentWork).where(AgentWork.workspace_id == workspace)))
        assert len(work) == 1
        assert work[0].payload["activity_id"] == str(source.id)
        assert work[0].payload["summary"] == body["summary"]
