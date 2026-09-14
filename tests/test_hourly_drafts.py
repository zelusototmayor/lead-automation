import importlib.util
from pathlib import Path
import pytest
P=Path(__file__).resolve().parents[1]/'ops/hourly-sales/head_of_sales_drafts.py'
spec=importlib.util.spec_from_file_location('hourly_drafts',P)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Fake:
    def __init__(self):self.drafts={};self.creates=0;self.fail=False
    def profile(self):return 'sales@example.test'
    def preflight(self,recipient,since,thread=None):
        if getattr(self,'human_draft',False):raise ValueError('Manual draft already exists')
        if getattr(self,'sent',False):raise ValueError('Outbound already exists since note')
    def signature(self):return '<p>Signature</p>'
    def lookup(self,marker):return list(self.drafts.values())
    def get(self,key):return self.drafts[key]
    def create(self,raw):
        self.creates+=1
        self.drafts['d1']={'id':'d1','message':{'id':'m1','raw':raw,'labelIds':['DRAFT']}}
        if self.fail:raise TimeoutError()
        return self.drafts['d1']

def test_retry_reconciles_after_uncertain_create(tmp_path):
    p=Fake();p.fail=True
    kwargs=dict(provider=p,journal=tmp_path/'journal',key='work:1',mailbox='sales@example.test',recipient='lead@example.test',subject='Follow-up',body_html='<p>Olá</p>')
    with pytest.raises(TimeoutError):m.create_verified_draft(**kwargs)
    p.fail=False
    result=m.create_verified_draft(**kwargs)
    assert result['draft_id']=='d1'
    assert m.create_verified_draft(**kwargs)==result
    assert p.creates==1

def test_literal_recipient_is_never_normalized(tmp_path):
    p=Fake()
    with pytest.raises(ValueError):m.create_verified_draft(provider=p,journal=tmp_path/'journal',key='w',mailbox='sales@example.test',recipient='relações@example.test',subject='Follow-up',body_html='<p>Olá</p>')
    assert p.creates==0

@pytest.mark.parametrize('flag',['human_draft','sent'])
def test_existing_manual_effect_prevents_a_second_draft(tmp_path,flag):
    p=Fake();setattr(p,flag,True)
    with pytest.raises(ValueError):
        m.create_verified_draft(provider=p,journal=tmp_path/'journal',key='w',
            mailbox='sales@example.test',recipient='lead@example.test',subject='Intro',body_html='<p>Intro</p>')
    assert p.creates==0


def test_provider_preflight_reads_sent_and_manual_drafts_without_mutation():
    provider=object.__new__(m.GmailDraftProvider)
    calls=[]
    def request(method,path,**kwargs):
        calls.append((method,path,kwargs))
        if path=='/messages':return {'messages':[]}
        if path=='/drafts':return {'drafts':[{'id':'human'}]}
        raise AssertionError(path)
    provider.request=request
    with pytest.raises(ValueError,match='draft'):
        provider.preflight('lead@example.test','2026-09-08T15:00:00+00:00')
    assert [x[1] for x in calls]==['/messages','/drafts']
    assert all(x[0]=='GET' for x in calls)


def test_reconcile_only_never_creates_when_journal_or_draft_is_missing(tmp_path):
    p=Fake();kwargs=dict(provider=p,journal=tmp_path/'journal',key='w',mailbox='sales@example.test',recipient='lead@example.test',subject='Follow-up',body_html='<p>Olá</p>')
    with pytest.raises(ValueError):m.create_verified_draft(**kwargs,reconcile_only=True)
    assert p.creates==0
    m.create_verified_draft(**kwargs)
    assert m.create_verified_draft(**kwargs,reconcile_only=True)['sent'] is False
    p.drafts.clear()
    with pytest.raises((KeyError,ValueError)):m.create_verified_draft(**kwargs,reconcile_only=True)
    assert p.creates==1


def test_ambiguous_or_truncated_thread_search_stops_before_draft():
    provider=object.__new__(m.GmailDraftProvider)
    def request(method,path,**kwargs):
        if path=='/messages':return {'messages':[{'id':'1'},{'id':'2'}]}
        key=path.rsplit('/',1)[1]
        return {'internalDate':key,'threadId':key,'payload':{'headers':[
            {'name':'To','value':'lead@example.test'}, {'name':'Subject','value':'Subject '+key},
            {'name':'Message-ID','value':'<'+key+'@example.test>'}]}}
    provider.request=request
    with pytest.raises(ValueError,match='thread'):provider.thread('lead@example.test')


def test_content_change_cannot_reuse_draft_key(tmp_path):
    p=Fake();kwargs=dict(provider=p,journal=tmp_path/'journal',key='w',mailbox='sales@example.test',recipient='lead@example.test',subject='Follow-up',body_html='<p>Olá</p>')
    m.create_verified_draft(**kwargs)
    with pytest.raises(ValueError):m.create_verified_draft(**(kwargs|{'subject':'Changed'}))
    assert p.creates==1
