from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
import pytest
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers
from src.crm.persistence.models import Lead, Activity

@pytest.mark.parametrize('initial,outcome,expected', [('new','connected','contacted'),('new','no_answer','new'),('qualified','connected','qualified'),('lost','connected','lost')])
def test_call_save_projects_only_confirmed_contact(lead_operations_api,initial,outcome,expected):
    client,engine,ws,lead_id,actor=lead_operations_api
    with Session(engine) as s,s.begin():
        lead=s.get(Lead,lead_id);lead.stage=initial
        lead.highest_stage_rank={'new':10,'qualified':30,'lost':90}[initial]
        s.flush();version=lead.version
    cid=uuid4();url=f'/api/v1/commands/leads/{lead_id}/log-call'
    payload={'command_id':str(cid),'expected_version':version,'outcome_code':outcome,'summary':'Nota de teste.'}
    r=client.post(url,json=payload,headers=_headers(cid));assert r.status_code==200,r.text
    assert client.post(url,json=payload,headers=_headers(cid)).json()['replayed']
    with Session(engine) as s:
        assert s.get(Lead,lead_id).stage==expected
        assert len(list(s.scalars(select(Activity).where(Activity.lead_id==lead_id,Activity.activity_type=='call'))))==1
