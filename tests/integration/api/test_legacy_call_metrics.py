from datetime import UTC, datetime
from uuid import uuid4
import pytest
from datetime import timedelta
from sqlalchemy.orm import Session
from src.crm.persistence.models import Activity, Lead
from tests.integration.api.test_call_cadence_api import details
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers


def test_legacy_connected_is_answered_but_other_dimensions_remain_unknown(lead_operations_api):
    client, _, _, lead_id, _ = lead_operations_api
    command=uuid4()
    r=client.post(f'/api/v1/commands/leads/{lead_id}/log-call',headers=_headers(command),json={
        'command_id':str(command),'expected_version':1,'outcome_code':'connected'})
    assert r.status_code==200,r.text
    data=client.get('/api/v1/pipeline/call-metrics').json()
    assert data['counts']['answered']==1
    assert data['coverage']['answer_unknown']==0
    assert data['coverage']['useful_unknown']==data['coverage']['decision_maker_unknown']==1
    assert data['counts']['useful']==data['counts']['decision_maker']==data['counts']['first_answered_confirmed']==0
    assert data['coverage']['legacy_history_unknown']==1
    assert data['deficit'] is None
    assert data['confirmed_deficit'] is None


@pytest.mark.parametrize('outcome,answer', [('connected','human_counterparty'),('no_answer','no_answer'),('voicemail','voicemail')])
def test_explicit_outcome_fills_unknown_answer_only(lead_operations_api, outcome, answer):
    client, _, _, lead_id, _ = lead_operations_api
    command=uuid4()
    r=client.post(f'/api/v1/commands/leads/{lead_id}/log-call',headers=_headers(command),json={
        'command_id':str(command),'expected_version':1,'outcome_code':outcome,
        'call_details':details(answer_kind='unknown')})
    assert r.status_code==200,r.text
    data=client.get('/api/v1/pipeline/call-metrics').json()
    assert data['coverage']['answer_unknown']==0
    assert data['counts']['answered']==int(answer=='human_counterparty')
    assert data['counts']['useful']==data['counts']['decision_maker']==0


def test_reviewed_legacy_note_is_content_bound_and_does_not_generalize(lead_operations_api, monkeypatch):
    import hashlib
    from sqlalchemy.orm import Session
    from src.crm.persistence.models import Activity, Lead
    from src.crm.services import call_metrics as module
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    activity_id=uuid4()
    summary='Falei com a receção; pediram para voltar a ligar.'
    monkeypatch.setattr(module, 'REVIEWED_LEGACY_ANSWERS', {
        str(activity_id): ('follow_up', hashlib.sha256(summary.encode()).hexdigest())}, raising=False)
    with Session(engine) as s, s.begin():
        for id_, text, outcome in [(activity_id,summary,'follow_up'), (uuid4(),summary,'follow_up'),
                (uuid4(),summary,'no_answer'),(uuid4(),summary,'voicemail')]:
            s.add(Activity(id=id_, workspace_id=workspace_id,lead_id=lead_id,
                account_id=s.get(Lead,lead_id).account_id, activity_type='call',title='Test',
                occurred_at=datetime.now(UTC),outcome_code=outcome,summary=text))
    data=client.get('/api/v1/pipeline/call-metrics').json()
    assert data['counts']['attempts']==4
    assert data['counts']['answered']==1
    assert data['coverage']['answer_unknown']==1
    assert data['counts']['first_answered_confirmed']==0
    monkeypatch.setattr(module,'REVIEWED_LEGACY_ANSWERS',{str(activity_id):('follow_up','changed')})
    data=client.get('/api/v1/pipeline/call-metrics').json()
    assert data['counts']['answered']==0
    assert data['coverage']['answer_unknown']==2


@pytest.mark.parametrize('kind', ['first_contact','follow_up','unknown'])
def test_contact_axis_is_saved_independently_even_when_not_answered(lead_operations_api, kind):
    client, _, _, lead_id, _=lead_operations_api
    command=uuid4()
    r=client.post(f'/api/v1/commands/leads/{lead_id}/log-call',headers=_headers(command),json={
        'command_id':str(command),'expected_version':1,'outcome_code':'no_answer',
        'call_details':details(answer_kind='no_answer',contact_kind=kind)})
    assert r.status_code==200,r.text
    assert r.json()['call_details']['contact_kind']==kind
    data=client.get('/api/v1/pipeline/call-metrics').json()
    assert data['contact_counts'][kind]==1
    assert data['contact_counts']['new_companies']==(1 if kind=='first_contact' else 0)
    assert data['counts']['answered']==0


def test_contact_axis_uses_before_event_not_current_stage_or_future_contact(lead_operations_api):
    client, engine, workspace_id, lead_id, _=lead_operations_api
    with Session(engine) as s, s.begin():
        lead=s.get(Lead,lead_id)
        lead.stage='contacted'
        def add(type_,when,**kw):
            s.add(Activity(workspace_id=workspace_id,lead_id=lead_id,account_id=lead.account_id,
                activity_type=type_,title='Test',occurred_at=datetime.fromisoformat(when),**kw))
        add('note','2026-09-05T10:00:00+00:00')
        add('call','2026-09-06T23:30:00+00:00',outcome_code='no_answer',call_details=details(answer_kind='no_answer',contact_kind='first_contact'))
        add('call','2026-09-07T10:00:00+00:00',outcome_code='connected',call_details=details(contact_kind='first_contact'))
        add('email_sent','2026-09-08T10:00:00+00:00')
    day=client.get('/api/v1/pipeline/call-metrics?date=2026-09-07').json()
    assert day['counts']['attempts']==2
    assert day['contact_counts']=={'first_contact':1,'follow_up':1,'unknown':0,'new_companies':1}
    assert day['counts']['answered']==1
    assert client.get('/api/v1/pipeline/call-metrics?date=2026-09-06').json()['counts']['attempts']==0


@pytest.mark.parametrize('prior_type', ['email_sent','email_received','meeting','call'])
def test_prior_external_contact_overrides_first_contact_claim(lead_operations_api,prior_type):
    client, engine, workspace_id, lead_id, _=lead_operations_api
    with Session(engine) as s,s.begin():
        lead=s.get(Lead,lead_id)
        now=datetime.now(UTC)
        for type_,when in [(prior_type,now-timedelta(days=1)),('call',now)]:
            s.add(Activity(workspace_id=workspace_id,lead_id=lead_id,account_id=lead.account_id,
                activity_type=type_,title='Test',occurred_at=when,outcome_code='connected' if type_=='call' else None,
                call_details=details(contact_kind='first_contact') if when==now else None))
    data=client.get('/api/v1/pipeline/call-metrics').json()
    assert data['contact_counts']=={'first_contact':0,'follow_up':1,'unknown':0,'new_companies':0}


def test_account_level_email_prevents_new_company_count(lead_operations_api):
    client, engine, workspace_id, lead_id, _=lead_operations_api
    with Session(engine) as s,s.begin():
        lead=s.get(Lead,lead_id)
        now=datetime.now(UTC)
        s.add(Activity(workspace_id=workspace_id,account_id=lead.account_id,
            activity_type='email_sent',title='Previous email',occurred_at=now-timedelta(days=1)))
        s.add(Activity(workspace_id=workspace_id,lead_id=lead_id,account_id=lead.account_id,
            activity_type='call',title='Test',occurred_at=now,outcome_code='connected',
            call_details=details(contact_kind='first_contact')))
    data=client.get('/api/v1/pipeline/call-metrics').json()
    assert data['contact_counts']['follow_up']==1
    assert data['contact_counts']['new_companies']==0
