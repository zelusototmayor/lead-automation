from sqlalchemy.orm import Session
from tests.integration.api.test_note_plan import work_api, prepare, headers
from src.crm.persistence.models import Lead
import pytest

@pytest.mark.parametrize('outcome,target,allowed',[(None,'contacted',False),(None,'lost',False),(None,'not_a_fit',False),('connected','contacted',True),('connected','lost',False),('not_interested','lost',True),('not_interested','not_a_fit',False),('no_answer','contacted',False)])
def test_stage_requires_matching_explicit_human_outcome(work_api,outcome,target,allowed):
    client,engine,ws,lead_id=work_api
    work,source,plan=prepare(work_api,summary='Ligar amanhã',outcome_code=outcome)
    plan.update(facts=[],actions=[],disposition={'target_stage':target,'quote':'Ligar amanhã'})
    uri=f'/api/v1/agent/work/{work}/note-plan'
    result=client.post(uri,json=plan,headers=headers())
    assert result.status_code==(200 if allowed else 409),result.text
    if allowed:assert client.post(uri,json=plan,headers=headers()).json()['replayed']
    with Session(engine) as s:assert s.get(Lead,lead_id).stage==(target if allowed else 'new')


def test_note_cannot_reopen_terminal_stage(work_api):
    client,engine,ws,lead_id=work_api
    with Session(engine) as s,s.begin():
        l=s.get(Lead,lead_id);l.stage='lost';l.highest_stage_rank=90
    work,source,plan=prepare(work_api,outcome_code='connected')
    plan.update(actions=[],disposition={'target_stage':'contacted','quote':'Atendeu a receção.'})
    assert client.post(f'/api/v1/agent/work/{work}/note-plan',json=plan,headers=headers()).status_code==409
    with Session(engine) as s:assert s.get(Lead,lead_id).stage=='lost'
