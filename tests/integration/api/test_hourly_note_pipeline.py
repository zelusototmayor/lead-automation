"""Real worker/adapter/API/PostgreSQL loop; only model generation is a fixture."""
from datetime import UTC,datetime
import json
from sqlalchemy.orm import Session
from sqlalchemy import select
from uuid import uuid4
from tests.integration.api.test_agent_work_api import work_api,headers
from tests.test_hourly_note_adapter import a
from tests.test_hourly_sales_drain import worker
from src.crm.persistence.models import Activity,Lead,AgentWork


def test_five_notes_flow_through_worker_adapter_api_and_durable_audit(work_api,tmp_path,monkeypatch):
    client,engine,ws,lead_id=work_api
    with Session(engine) as s,s.begin():
        lead=s.get(Lead,lead_id)
        for _ in range(5):
            s.add(Activity(id=uuid4(),workspace_id=ws,lead_id=lead_id,account_id=lead.account_id,
                activity_type='call',title='Fixture call',summary='Atendeu a receção.',
                occurred_at=datetime.now(UTC),source_system='manual',actor_type='user'))
    class Bridge:
        requests=0
        def call(self,method,path,payload=None):
            self.requests+=1
            result=client.request(method,path,json=payload,headers=headers())
            assert result.status_code==200,result.text
            return result.json()
    bridge=Bridge();path=tmp_path/'state.sqlite3';store=a.Store(path)
    cfg={'enabled':True,'hourly_drain':True,'sync_mode':'disabled','state_file':str(path)}
    config=tmp_path/'config.json';config.write_text(json.dumps(cfg))
    monkeypatch.setattr(worker,'run_sensor',lambda path:a.collect(bridge,store,cfg))
    def interpret(batch,run_dir):
        for item in batch['items']:
            source=item['context']['note_source']
            plan={'expected_lead_version':item['context']['lead_version'],'source_digest':source['source_digest'],
                  'facts':[{'field':'answer_kind','value':'human_counterparty','quote':'Atendeu a receção.'}],
                  'actions':[],'summary':'Fixture interpretation; no external effects'}
            a.note_plan(bridge,store,item['id'],plan,cfg)
        return {'ok':True,'contract':{'notify_jose':False,'summary':''}}
    monkeypatch.setattr(worker,'run_model',interpret)
    actual=worker.receipts
    def receipts(items,state_path=None):
        assert state_path==path,'Worker must use the configured isolated receipt store'
        return actual(items,state_path=path)
    monkeypatch.setattr(worker,'receipts',receipts)
    assert worker.execute(config,tmp_path/'ops')==[]
    result=json.loads((tmp_path/'ops/last-run.json').read_text())
    assert result['processed']==5 and result['batches']==3 and result['drained'] is True
    assert result['audit']['count']==5 and result['audit']['complete'] is True
    with Session(engine) as s:
        work=list(s.scalars(select(AgentWork).where(AgentWork.workspace_id==ws)))
        assert len(work)==5 and all(row.status=='completed' for row in work)
    # Cold restart/replay audits again but does not process any note twice.
    assert worker.execute(config,tmp_path/'ops')==[]
    assert json.loads((tmp_path/'ops/last-run.json').read_text())['processed']==0
    store.close()
