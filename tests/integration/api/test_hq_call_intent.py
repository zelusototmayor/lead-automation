from datetime import UTC, datetime, timedelta
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers
from src.crm.persistence.models import Task, AgentWork


def test_internal_call_scheduling_persists_intent_without_calendar_work(lead_operations_api):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    command = uuid4()
    intent = dict(schema_version=1,purpose='internal_preparation',agreed_with_client=False,
                  calendar_policy='none',obligation_key='internal-preparation:test',evidence_refs=[],gate_reason=None)
    payload = dict(command_id=str(command),expected_version=1,task_type='call',title='Preparar chamada',
                   due_at=(datetime.now(UTC)+timedelta(days=1)).isoformat(),call_intent=intent)
    url = f'/api/v1/commands/leads/{lead_id}/schedule-next-action'
    result = client.post(url,json=payload,headers=_headers(command))
    assert result.status_code == 200, result.text
    assert result.json().get('callback_sync_status') == 'not_required'
    assert client.post(url,json=payload,headers=_headers(command)).json()['replayed'] is True
    with Session(engine) as session:
        task = session.scalars(select(Task).where(Task.workspace_id==workspace_id)).one()
        assert task.call_intent == intent
        assert not session.scalars(select(AgentWork).where(AgentWork.workspace_id==workspace_id,AgentWork.kind=='calendar_callback')).all()
