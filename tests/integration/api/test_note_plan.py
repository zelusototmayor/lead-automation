from datetime import UTC, datetime, timedelta
from uuid import uuid4
from sqlalchemy.orm import Session
from sqlalchemy import select
from tests.integration.api.test_agent_work_api import work_api, headers
from src.crm.persistence.models import Activity, Lead, Task, AgentWork, AuditEvent
from src.crm.services.note_source_service import enqueue_note_source


def prepare(work_api, summary='Atendeu a receção. Pediu para ligar amanhã.', call_details=None, outcome_code=None):
    client,engine,ws,lead_id=work_api
    with Session(engine) as s, s.begin():
        lead=s.get(Lead,lead_id)
        source=Activity(id=uuid4(),workspace_id=ws,account_id=lead.account_id,lead_id=lead_id,
            activity_type='call',title='Call',summary=summary,call_details=call_details,outcome_code=outcome_code,
            occurred_at=datetime.now(UTC),source_system='manual',actor_type='user')
        s.add(source);s.flush()
        work=enqueue_note_source(s,source);source_id=source.id
    item=client.post('/api/v1/agent/work/claim',headers=headers(),json={'worker_id':'test','limit':1}).json()['items'][0]
    plan={'lease_token':item['lease_token'],'expected_lead_version':item['context']['lead_version'],
        'source_digest':item['context']['note_source']['source_digest'],
        'facts':[{'field':'answer_kind','value':'human_counterparty','quote':'Atendeu a receção.'}],
        'actions':[{'key':'callback','task_type':'call','title':'Ligar amanhã',
            'due_at':(datetime.now(UTC)+timedelta(days=1)).isoformat(),'quote':'Pediu para ligar amanhã.'}],
        'summary':'Atendimento na receção; callback interno.'}
    return work,source_id,plan


def test_note_plan_creates_one_task_and_keeps_source_immutable(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    uri=f'/api/v1/agent/work/{work}/note-plan'
    r=client.post(uri,json=plan,headers=headers())
    assert r.status_code==200,r.text
    result=r.json()
    assert result['result']['facts']['answer_kind']=='human_counterparty'
    assert client.post(uri,json=plan,headers=headers()).json()['replayed'] is True
    with Session(engine) as s:
        source=s.get(Activity,source_id)
        assert source.call_details is None
        assert source.summary=='Atendeu a receção. Pediu para ligar amanhã.'
        tasks=list(s.scalars(select(Task).where(Task.lead_id==lead)))
        assert len(tasks)==1
        assert tasks[0].task_type=='call'
        assert tasks[0].source_rule=='head_of_sales_note'
        assert s.get(AgentWork,work).status=='completed'
        from src.crm.services.call_metrics import call_metrics
        metrics=call_metrics(s,ws,datetime.now(UTC).date())
        assert metrics['counts']['answered']==1


def test_metrics_ignores_projection_with_wrong_source_digest(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    with Session(engine) as s,s.begin():
        s.add(AuditEvent(id=uuid4(),workspace_id=ws,command_id=uuid4(),actor_id=uuid4(),
            action='agent.note_interpreted',entity_type='activity',entity_id=source_id,
            details={'source_digest':'0'*64,'facts':{'answer_kind':'human_counterparty'}}))
    with Session(engine) as s:
        from src.crm.services.call_metrics import call_metrics
        assert call_metrics(s,ws,datetime.now(UTC).date())['counts']['answered']==0


def test_model_cannot_forge_a_verified_gmail_receipt(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    action=plan['actions'][0]|{'task_type':'email','draft_receipt':{
        'draft_id':'invented','message_id':'invented','status':'draft',
        'fingerprint':'a'*64,'url':'https://mail.google.com/mail/u/0/#drafts',
        'verified_by':'Gmail drafts.get raw MIME','sent':False}}
    response=client.post(f'/api/v1/agent/work/{work}/note-plan',
        json=plan|{'actions':[action]},headers=headers())
    assert response.status_code==422,response.text
    with Session(engine) as s:
        assert not list(s.scalars(select(Task).where(Task.lead_id==lead)))
        assert s.get(AgentWork,work).status=='running'


def test_draft_execution_is_server_owned_and_after_all_validation(work_api, monkeypatch):
    from src.crm.services import note_plan_service as service
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    calls=[]
    def provider_boundary(**kwargs):
        calls.append(kwargs)
        return {'draft_id':'provider-id','message_id':'provider-message', 'status':'draft',
                'verified_by':'Gmail drafts.get raw MIME','sent':False}
    monkeypatch.setattr(service,'draft_for_action',provider_boundary,raising=False)
    action=plan['actions'][0]|{'task_type':'email','draft':{
        'recipient':'ana@example.test','subject':'Intro','body_html':'<p>Intro</p>'}}
    uri=f'/api/v1/agent/work/{work}/note-plan'
    bad=plan|{'actions':[action,plan['actions'][0]|{'key':'bad','quote':'absent'}]}
    assert client.post(uri,json=bad,headers=headers()).status_code==409
    assert calls==[]
    response=client.post(uri,json=plan|{'actions':[action]},headers=headers())
    assert response.status_code==200,response.text
    assert len(calls)==1
    assert response.json()['result']['tasks'][0]['draft']['draft_id']=='provider-id'
    assert client.post(uri,json=plan|{'actions':[action]},headers=headers()).status_code==200
    assert len(calls)==1


def test_note_cannot_be_completed_by_unverified_generic_finish(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    response=client.post(f'/api/v1/agent/work/{work}/finish',headers=headers(),json={
        'lease_token':plan['lease_token'],'status':'completed',
        'result':{'summary':'Model claims draft exists','evidence':[{'provider':'gmail','draft_id':'fake'}]}})
    assert response.status_code==409
    waiting=client.post(f'/api/v1/agent/work/{work}/finish',headers=headers(),json={
        'lease_token':plan['lease_token'],'status':'waiting',
        'result':{'summary':'Recipient unvalidated','evidence':[],
            'next_action':{'owner':'head-of-sales','waiting_reason':'Literal spelling unvalidated'}}})
    assert waiting.status_code==200


def test_note_callback_uses_existing_calendar_queue_and_exact_source(work_api):
    from src.crm.services.callback_execution import callback_content
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    response=client.post(f'/api/v1/agent/work/{work}/note-plan',json=plan,headers=headers())
    assert response.status_code==200,response.text
    with Session(engine) as s:
        tasks=list(s.scalars(select(Task).where(Task.lead_id==lead)))
        callbacks=list(s.scalars(select(AgentWork).where(AgentWork.workspace_id==ws,AgentWork.kind=='calendar_callback')))
        assert len(callbacks)==1
        assert callbacks[0].task_id==tasks[0].id
        content=callback_content(s,tasks[0])
        assert content['company_name']=='Real Contact'
        assert content['call_notes']=='Atendeu a receção. Pediu para ligar amanhã.'
        assert response.json()['result']['tasks'][0]['calendar_status']=='pending'


def test_note_status_remains_partial_until_callback_receipt(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    response=client.post(f'/api/v1/agent/work/{work}/note-plan',json=plan,headers=headers())
    assert response.status_code==200
    current=client.get(f'/api/v1/agent/work/{work}',headers=headers()).json()
    assert current['processing']['state']=='partial'
    callback_id=response.json()['result']['tasks'][0]['calendar_work_id']
    with Session(engine) as s,s.begin():
        callback=s.get(AgentWork,callback_id)
        callback.status='completed'
        callback.result={'evidence':[{'provider':'google_calendar','event_id':'fixture','verified':True}]}
    current=client.get(f'/api/v1/agent/work/{work}',headers=headers()).json()
    assert current['processing']['state']=='processed'
    assert current['processing']['obligations'][0]['calendar_status']=='verified'


def test_conflicting_audit_projections_remain_unknown(work_api):
    from src.crm.services.call_metrics import call_metrics
    from src.crm.services.note_source_service import source_digest
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    with Session(engine) as s,s.begin():
        digest=source_digest(s.get(Activity,source_id))
        for value in ('no_answer','human_counterparty'):
            s.add(AuditEvent(id=uuid4(),workspace_id=ws,command_id=uuid4(),actor_id=uuid4(),
                action='agent.note_interpreted',entity_type='activity',entity_id=source_id,
                details={'source_digest':digest,'facts':{'answer_kind':value}}))
    with Session(engine) as s:
        metrics=call_metrics(s,ws,datetime.now(UTC).date())
        assert metrics['counts']['answered']==0
        assert metrics['coverage']['answer_unknown']==1


def test_server_draft_recovers_sql_rollback_without_second_provider_create(work_api,monkeypatch,tmp_path):
    from tests.test_hourly_drafts import Fake
    from src.crm.connectors import note_drafts
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api,summary='Atendeu a receção. Pediu apresentação por email.')
    provider=Fake();provider.fail=True
    provider.profile=lambda:'zelu@zelusottomayor.com'
    monkeypatch.setattr(note_drafts,'GmailDraftProvider',lambda *a:provider)
    monkeypatch.setenv('CRM_GOOGLE_MAILBOX','zelu@zelusottomayor.com')
    monkeypatch.setenv('GOOGLE_GMAIL_CREDENTIALS_FILE','fixture-not-a-credential')
    monkeypatch.setenv('CRM_NOTE_DRAFT_JOURNAL_DIR',str(tmp_path))
    plan['actions']=[plan['actions'][0]|{'task_type':'email','quote':'Pediu apresentação por email.',
        'draft':{'recipient':'ana@example.test','subject':'Apresentação','body_html':'<p>Olá</p>'}}]
    uri=f'/api/v1/agent/work/{work}/note-plan'
    assert client.post(uri,json=plan,headers=headers()).status_code==409
    with Session(engine) as s:
        assert not list(s.scalars(select(Task).where(Task.lead_id==lead)))
        assert s.get(AgentWork,work).status=='running'
    provider.fail=False
    reply=client.post(uri,json=plan,headers=headers())
    assert reply.status_code==200,reply.text
    assert provider.creates==1
    assert reply.json()['result']['tasks'][0]['draft']['sent'] is False
    assert client.post(uri,json=plan,headers=headers()).json()['replayed'] is True
    assert provider.creates==1
    assert client.get('/api/v1/agent/notes/audit',headers=headers()).json()['items'][0]['processing']['state']=='processed'
    provider.drafts.clear()
    audit=client.get('/api/v1/agent/notes/audit',headers=headers()).json()['items'][0]['processing']
    assert audit['state']=='blocked'
    assert audit['obligations'][0]['draft_status']=='unconfirmed'
    assert provider.creates==1


def test_human_facts_win_over_different_projection(work_api):
    from src.crm.domain.call_contract import CallDetails
    from src.crm.services.call_metrics import call_metrics
    from src.crm.services.note_source_service import source_digest
    details=CallDetails(schema_version=1,attempted=True,answer_kind='no_answer',useful=None,
        decision_maker=None,interlocutor_role='unknown',repeat_reason=None).model_dump(mode='json')
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api,call_details=details)
    assert client.post(f'/api/v1/agent/work/{work}/note-plan',json=plan,headers=headers()).status_code==409
    with Session(engine) as s,s.begin():
        s.add(AuditEvent(id=uuid4(),workspace_id=ws,command_id=uuid4(),actor_id=uuid4(),
            action='agent.note_interpreted',entity_type='activity',entity_id=source_id,
            details={'source_digest':source_digest(s.get(Activity,source_id)),'facts':{'answer_kind':'human_counterparty'}}))
    with Session(engine) as s:
        assert call_metrics(s,ws,datetime.now(UTC).date())['counts']['answered']==0


def test_next_action_cannot_bypass_source_bound_note_plan(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    response=client.post(f'/api/v1/agent/work/{work}/next-action',headers=headers(),json={
        'lease_token':plan['lease_token'],'expected_lead_version':plan['expected_lead_version'],
        'title':'Pretend email prepared','due_at':plan['actions'][0]['due_at'],'task_type':'email'})
    assert response.status_code==409
    with Session(engine) as s:
        assert not list(s.scalars(select(Task).where(Task.lead_id==lead)))


def test_claim_task_context_exposes_version_for_safe_adoption(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    with Session(engine) as s,s.begin():
        target=Task(id=uuid4(),workspace_id=ws,lead_id=lead,task_type='call',title='Human callback',
            due_at=datetime.now(UTC)+timedelta(days=1),status='open',owner_user_id=uuid4())
        s.add(target);s.flush();task_id=str(target.id);version=target.version
    context=client.get(f'/api/v1/agent/work/{work}',headers=headers()).json()['context']
    assert context['open_tasks'][0]['version']==version
    assert context['open_tasks'][0]['status']=='open'
    plan['actions'][0].update(existing_task_id=task_id,expected_task_version=version)
    assert client.post(f'/api/v1/agent/work/{work}/note-plan',json=plan,headers=headers()).status_code==200
    with Session(engine) as s:
        assert len(list(s.scalars(select(Task).where(Task.lead_id==lead))))==1


def test_newer_human_note_blocks_old_source_actions_without_stage_guess(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    with Session(engine) as s,s.begin():
        s.add(Activity(id=uuid4(),workspace_id=ws,lead_id=lead,account_id=s.get(Lead,lead).account_id,activity_type='note',title='Correction',
            summary='Não contactar até validar. Recusa registada.',occurred_at=datetime.now(UTC),
            source_system='manual',actor_type='user'))
    reply=client.post(f'/api/v1/agent/work/{work}/note-plan',json=plan,headers=headers())
    assert reply.status_code==409
    context=client.get(f'/api/v1/agent/work/{work}',headers=headers()).json()['context']['note_source']
    assert context['newer_context']['items'][0]['summary']=='Não contactar até validar. Recusa registada.'
    with Session(engine) as s:
        assert not list(s.scalars(select(Task).where(Task.lead_id==lead)))


def test_note_plan_rejects_stale_source_and_human_edit(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    uri=f'/api/v1/agent/work/{work}/note-plan'
    assert client.post(uri,json=plan|{'source_digest':'0'*64},headers=headers()).status_code==409
    with Session(engine) as s,s.begin():
        row=s.get(Lead,lead);row.company_name='Human edit'
    assert client.post(uri,json=plan,headers=headers()).status_code==409


def test_note_plan_rejects_ungrounded_quote_and_sent_task(work_api):
    client,engine,ws,lead=work_api
    work,source_id,plan=prepare(work_api)
    uri=f'/api/v1/agent/work/{work}/note-plan'
    bad=plan|{'facts':[{'field':'decision_maker','value':True,'quote':'Falei com o decisor.'}]}
    assert client.post(uri,json=bad,headers=headers()).status_code==409
    assert client.post(uri,json=plan|{'actions':[plan['actions'][0]|{'task_type':'send'}]},headers=headers()).status_code==422
