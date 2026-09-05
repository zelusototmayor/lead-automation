"""Release import truth/suppression and human create command regression tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4, UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session, sessionmaker

from scripts.crm_release_import import import_workbook, digest, proposal_evidence
from dashboard.app.routers import lead_create
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import Workspace, Account, Contact, Lead, Task, Activity, SourceIdentity, IngestEvent, Proposal, AuditEvent, OutboxEvent
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.migration._postgres import require_disposable_postgres, cleanup_workspace

HEADERS=['Company','Email','Stage','Contact','Due','Due Time','notes','Proposal Sent','Proposal Status','Proposal Outcome','Proposal Next Action','Outcome']

def workbook(*rows):
    return {'spreadsheet_id':'synthetic-frozen-sheet','captured_at':'2026-05-01T12:00:00+01:00','worksheets':[{'name':'PT Logistics','values':[HEADERS]+[[r.get(k,'') for k in HEADERS] for r in rows]},{'name':'History','values':[['raw'],['complete historical archive']]}]}

@pytest.fixture
def db():
    engine=create_engine(require_disposable_postgres());workspace=uuid4();owner=uuid4()
    with Session(engine) as s,s.begin():s.add(Workspace(id=workspace,slug='infra-'+str(workspace),name='Infrastructure test'))
    yield engine,workspace,owner
    cleanup_workspace(engine,workspace);engine.dispose()

def run_import(db,book):
    engine,workspace,owner=db
    with Session(engine) as s,s.begin():return import_workbook(book,s,workspace,owner)

def counts(db,model):
    engine,workspace,_=db
    with Session(engine) as s:return s.scalar(select(func.count()).select_from(model).where(model.workspace_id==workspace))

@pytest.mark.parametrize('reverse',[False,True])
def test_suppressed_duplicate_email_cannot_reactivate_in_either_order(db,reverse):
    rows=[{'Company':'Active stale row','Email':'Same@Example.test','Stage':'Send Email','Due':'2026-12-16'}, {'Company':'Refused contact','Email':'same@example.test','Stage':'Not a Fit'}]
    report,_=run_import(db,workbook(*(rows[::-1] if reverse else rows)));assert report['suppressed']==2
    engine,workspace,_=db
    with Session(engine) as s:
        assert {x.stage for x in s.scalars(select(Lead).where(Lead.workspace_id==workspace))}=={'not_a_fit'}
        assert {x.status for x in s.scalars(select(Contact).where(Contact.workspace_id==workspace))}=={'inactive'}
        assert len(list(s.scalars(select(Contact).where(Contact.workspace_id==workspace,Contact.primary_email=='same@example.test'))))==1
        assert not list(s.scalars(select(Task).where(Task.workspace_id==workspace)))
        ids=list(s.scalars(select(SourceIdentity).where(SourceIdentity.workspace_id==workspace,SourceIdentity.entity_kind=='lead')))
        assert all(x.metadata_json['suppressed'] for x in ids)
        assert all(x.metadata_json['normalized_email']=='same@example.test' for x in ids)


def test_quarantined_refusal_protects_email_in_other_row_and_is_archived(db):
    report,quarantine=run_import(db,workbook({'Company':'','Email':'blocked@example.test','Stage':'Lost'}, {'Company':'Later row','Email':'blocked@example.test','Stage':'New'}))
    assert report['quarantined']==1;assert report['suppressed']==1;assert quarantine[0]['reason']=='missing_company'
    engine,workspace,_=db
    with Session(engine) as s:
        source=s.scalar(select(SourceIdentity).where(SourceIdentity.workspace_id==workspace,SourceIdentity.external_id=='row:2'))
        assert source.canonical_entity_id is None;assert source.metadata_json['suppressed']
        assert source.metadata_json['legacy_row']['Email']=='blocked@example.test'
        assert counts(db,IngestEvent)==2


def test_initial_email_hack_does_not_create_proposal_and_closed_dates_not_invented(db):
    report,quarantine=run_import(db,workbook(
        {'Company':'Outreach','Email':'out@example.test','Stage':'Email Sent','Proposal Sent':'2026-04-01'},
        {'Company':'Real proposal','Email':'proposal@example.test','Stage':'Proposal Sent','Proposal Sent':'2026-04-10'},
        {'Company':'Closed proposal','Email':'closed@example.test','Stage':'Lost','Proposal Sent':'2026-04-11','Proposal Outcome':'Lost'},
        {'Company':'New request','Stage':'Email Sent','Proposal Sent':'2026-04-12','notes':'É necessário enviar proposta'}))
    assert report['proposals']==1;assert report['ambiguous_proposal_dates_archived']==2;assert report['closed_proposals_without_close_date_archived']==1
    assert any(x['reason']=='closed_proposal_missing_close_date' for x in quarantine)
    engine,workspace,_=db
    with Session(engine) as s:
        p=s.scalar(select(Proposal).where(Proposal.workspace_id==workspace));assert p.status=='sent';assert p.sent_at.date().isoformat()=='2026-04-10';assert p.lost_at is None;assert p.value_state=='missing'
    assert proposal_evidence({'Proposal Sent':'2026-04-01'}) is None


def test_future_email_does_not_erase_or_accelerate_callback(db):
    report,_=run_import(db,workbook({'Company':'Future','Email':'future@example.test','Stage':'Send Email','Due':'2026-12-16','Due Time':'14:30'}))
    assert report['callbacks']==report['email_tasks']==1
    engine,workspace,_=db
    with Session(engine) as s:
        tasks=list(s.scalars(select(Task).where(Task.workspace_id==workspace)));assert {t.task_type for t in tasks}=={'call','email'};assert len({t.due_at for t in tasks})==1;assert tasks[0].due_at.date().isoformat()=='2026-12-16'


def test_replay_preserves_human_edits_and_changed_archive_is_rejected(db):
    book=workbook({'Company':'Original','Email':'first@example.test','Stage':'New','notes':'Historical note'})
    run_import(db,book);engine,workspace,_=db
    with Session(engine) as s,s.begin():
        lead=s.scalar(select(Lead).where(Lead.workspace_id==workspace));lead.company_name='Human correction'
    replay,_=run_import(db,book);assert replay['replay_noop']==1
    changed=deepcopy(book);changed['worksheets'][0]['values'][1][0]='Other source content'
    with pytest.raises(ValueError,match='different workbook snapshot'):run_import(db,changed)
    with Session(engine) as s:
        assert s.scalar(select(Lead).where(Lead.workspace_id==workspace)).company_name=='Human correction'
        archive=s.scalar(select(SourceIdentity).where(SourceIdentity.workspace_id==workspace,SourceIdentity.external_id=='release-archive'))
        assert archive.metadata_json['snapshot_sha256']==digest(book)
        assert archive.metadata_json['all_worksheet_names']==['PT Logistics','History']
        activity=s.scalar(select(Activity).where(Activity.workspace_id==workspace));assert activity.occurred_at.astimezone(timezone.utc).isoformat()=='2026-05-01T11:00:00+00:00';assert 'data da interação original é desconhecida' in activity.summary
        lead=s.scalar(select(Lead).where(Lead.workspace_id==workspace));assert lead.created_at>datetime(2026,5,1,tzinfo=timezone.utc)


def principal(db):return CRMPrincipal(workspace_id=db[1],subject='synthetic-human',actor_id=db[2],permissions=frozenset({'crm:lead:create'}))

def create(db,body,who=None):
    semantic=hashlib.sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with SqlAlchemyUnitOfWork(sessionmaker(db[0])) as uow:
        outcome=lead_create.apply_create(uow,body,who or principal(db),semantic);uow.commit();return outcome


def test_new_human_create_cannot_bypass_quarantined_suppression(db):
    run_import(db,workbook({'Company':'','Email':'protected@example.test','Stage':'Lost'}))
    body=lead_create.CreateLead(command_id=uuid4(),company='Different company',email='PROTECTED@example.test')
    with pytest.raises(HTTPException) as exc:create(db,body)
    assert exc.value.status_code==409;assert counts(db,Lead)==0


def test_new_human_create_cannot_reuse_denormalized_only_lead_email(db):
    engine,workspace,owner=db
    with Session(engine) as s,s.begin():s.add(Lead(workspace_id=workspace,company_name='Older lead',contact_email='existing@example.test'))
    with pytest.raises(HTTPException) as exc:create(db,lead_create.CreateLead(command_id=uuid4(),company='Another',email='existing@example.test'))
    assert exc.value.status_code==409;assert counts(db,Contact)==0


def test_create_concurrent_idempotency_actor_and_payload_are_fenced(db):
    body=lead_create.CreateLead(command_id=uuid4(),company='New company',email='human@example.test',phone='+351 999 123 456',notes='Human context')
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:create(db,body),[1,2]))
    assert {r['replayed'] for r in results}=={False,True};assert len({r['lead_id'] for r in results})==1
    assert counts(db,Lead)==counts(db,Activity)==counts(db,AuditEvent)==counts(db,OutboxEvent)==1
    with pytest.raises(HTTPException):create(db,body.model_copy(update={'company':'Changed payload'}))
    other=CRMPrincipal(workspace_id=db[1],subject='other',actor_id=uuid4(),permissions=frozenset({'crm:lead:create'}))
    with pytest.raises(HTTPException):create(db,body,other)
    with Session(db[0]) as s:
        lead=s.get(Lead,UUID(results[0]['lead_id']));assert lead.company_name=='New company';assert lead.contact_email=='human@example.test'


def test_create_permissions_csrf_origin_and_idempotency_header(db,monkeypatch):
    import dashboard.app.security as security
    monkeypatch.setattr(security,'get_settings',lambda:SimpleNamespace(csrf_token='synthetic-csrf',allowed_write_origins={'https://crm.example.test'}))
    monkeypatch.setattr(lead_create,'require_postgres_command_writer',lambda:None)
    app=FastAPI();app.include_router(lead_create.router);app.dependency_overrides[require_crm_principal]=lambda:principal(db)
    body={'command_id':str(uuid4()),'company':'API-created'};headers={'X-CSRF-Token':'synthetic-csrf','Origin':'https://crm.example.test','Idempotency-Key':body['command_id']}
    with TestClient(app) as client:
        assert client.post('/api/v1/commands/leads',json=body).status_code==403
        assert client.post('/api/v1/commands/leads',json=body,headers=headers|{'Origin':'https://evil.example.test'}).status_code==403
        assert client.post('/api/v1/commands/leads',json=body,headers=headers|{'Idempotency-Key':str(uuid4())}).status_code==409
        assert client.post('/api/v1/commands/leads',json=body,headers=headers).status_code==200
        assert client.post('/api/v1/commands/leads',json=body,headers=headers).json()['replayed'] is True
    assert counts(db,Lead)==1


def test_identity_validation_rejects_format_control_and_multi_line_names():
    for company in ['\u200bInvisible','Line\nBreak']:
        with pytest.raises(ValidationError):lead_create.CreateLead(command_id=uuid4(),company=company)
    with pytest.raises(ValidationError):lead_create.CreateLead(command_id=uuid4(),company='Good',email='a@example.test;b@example.test')
