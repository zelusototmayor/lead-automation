from datetime import datetime, UTC
from uuid import uuid4
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers


def test_prepare_day_is_canonical_idempotent_and_writes_no_call_tasks(lead_operations_api):
    client, engine, workspace_id, lead_id, _=lead_operations_api
    command=uuid4()
    day=datetime.now(UTC).date().isoformat()
    data=dict(command_id=str(command),work_date=day,expected_version=0,capacity_minutes=60)
    url='/api/v1/commands/pipeline/prepare-call-day'
    first=client.post(url,json=data,headers=_headers(command))
    assert first.status_code==200,first.text
    second=client.post(url,json=data,headers=_headers(command))
    assert second.status_code==200 and second.json()['replayed'] is True
    plan=client.get(f'/api/v1/pipeline/call-day?date={day}').json()
    assert plan['command_id']==str(command)
    assert plan['version']==1
    assert plan['capacity_minutes']==60
    assert plan['target_first_answered']==10
    assert plan['new_tasks_created']==0
    assert plan['calendar_events_created']==0
    assert plan['source_status']=='partial'
    assert client.post(url,json=data|{'capacity_minutes':90},headers=_headers(command)).status_code==409


def test_preparation_excludes_closed_leads_and_deduplicates_shared_phones(lead_operations_api):
    from sqlalchemy.orm import Session
    from src.crm.persistence.models import Lead
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        lead=session.get(Lead,lead_id); lead.contact_phone='+351211234567'; lead.stage='lost'
    command=uuid4(); day=datetime.now(UTC).date().isoformat()
    reply=client.post('/api/v1/commands/pipeline/prepare-call-day',headers=_headers(command),
       json=dict(command_id=str(command),work_date=day,expected_version=0,capacity_minutes=60))
    assert reply.status_code==200,reply.text
    assert not reply.json()['items'], 'Closed leads must not be recommended as new prospects'

