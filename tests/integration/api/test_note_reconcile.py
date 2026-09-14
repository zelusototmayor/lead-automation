from datetime import UTC,datetime,timedelta
from uuid import uuid4
from sqlalchemy.orm import Session
from sqlalchemy import select
from tests.integration.api.test_agent_work_api import work_api,headers
from src.crm.persistence.models import Activity,Lead,AgentWork


def test_note_audit_pages_cover_all_dispositions_without_claiming(work_api):
    client,engine,ws,lead_id=work_api
    from src.crm.services.agent_work_service import enqueue_work
    with Session(engine) as s,s.begin():
        ids=[str(enqueue_work(s,workspace_id=ws,lead_id=lead_id,kind='call_followup',source_key=str(uuid4()))) for _ in range(5)]
    response=client.get('/api/v1/agent/notes/audit?limit=2',headers=headers())
    assert response.status_code==200,response.text
    data=response.json();items=data['items']
    while data['next_cursor']:
        data=client.get('/api/v1/agent/notes/audit',params={'limit':2,'cursor':data['next_cursor'],'cutoff':data['cutoff']},headers=headers()).json()
        items+=data['items']
    assert {x['id'] for x in items}==set(ids)
    assert len(items)==5
    assert all(x['processing']['state']=='pending' for x in items)
    with Session(engine) as s:
        assert all(r.status=='queued' and r.attempts==0 for r in s.scalars(select(AgentWork).where(AgentWork.workspace_id==ws)))


def test_reconcile_covers_old_notes_in_batches_and_replay_is_empty(work_api):
    client,engine,ws,lead_id=work_api
    source_ids=[]
    with Session(engine) as s,s.begin():
        lead=s.get(Lead,lead_id)
        for i in range(5):
            a=Activity(id=uuid4(),workspace_id=ws,lead_id=lead_id,account_id=lead.account_id,
                activity_type='note',title='Historical note',summary='Manual note',
                occurred_at=datetime.now(UTC)-timedelta(days=30+i),source_system='manual',actor_type='user')
            s.add(a);source_ids.append(str(a.id))
    uri='/api/v1/agent/notes/reconcile'
    first=client.post(uri,headers=headers(),json={'limit':2})
    assert first.status_code==200,first.text
    replies=[first.json()]+[client.post(uri,headers=headers(),json={'limit':2}).json() for _ in range(3)]
    assert [r['enqueued'] for r in replies]==[2,2,1,0]
    assert [r['has_more'] for r in replies]==[True,True,False,False]
    with Session(engine) as s:
        rows=list(s.scalars(select(AgentWork).where(AgentWork.workspace_id==ws)))
        assert len(rows)==5
        assert {r.payload['activity_id'] for r in rows}==set(source_ids)
        assert all(len(r.payload['source_digest'])==64 and r.status=='queued' for r in rows)
