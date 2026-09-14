import importlib.util
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]/'ops/hourly-sales'
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('hourly_adapter_test',ROOT/'head_of_sales_adapter.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


def test_business_window_limits_model_not_calendar(tmp_path,monkeypatch):
    real=a.dt.datetime
    class Night(real):
        @classmethod
        def now(cls,tz=None):return real(2026,9,14,2,tzinfo=a.dt.timezone.utc).astimezone(tz)
    monkeypatch.setattr(a.dt,'datetime',Night)
    store=a.Store(tmp_path/'state')
    class Client:
        requests=0
        def __init__(self):self.calls=[]
        def call(self,method,path,payload=None):
            self.calls.append((path,payload));self.requests+=1
            return {'items':[], 'next_cursor':None,'cutoff':'2026-09-14T02:00:00+00:00'}
    c=Client();a.collect(c,store,{'hourly_drain':True,'sync_mode':'disabled'})
    kinds=[body['kinds'] for path,body in c.calls if body and 'kinds' in body]
    assert kinds==[['calendar_callback']]


def test_note_plan_cli_is_exposed():
    import subprocess
    result=subprocess.run([sys.executable,str(ROOT/'head_of_sales_adapter.py'),'note-plan','--help'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert '--plan-file' in result.stdout


def test_hourly_collect_has_no_silent_daily_cap_and_reconciles(tmp_path):
    store=a.Store(tmp_path/'state');store.count('reasoning_claims',200)
    class Client:
        requests=0
        def __init__(self):self.calls=[]
        def call(self,method,path,payload=None):
            self.calls.append((path,payload));self.requests+=1
            if path.endswith('/reconcile'):return {'enqueued':0,'has_more':False}
            if payload and 'call_followup' in payload.get('kinds',[]):
                return {'items':[{'id':'note1','kind':'call_followup','lease_token':'l','payload':{}}]}
            return {'items':[]}
    c=Client()
    result=a.collect(c,store,{'hourly_drain':True,'sync_mode':'disabled'})
    assert result['wakeAgent'] is True
    assert any(path.endswith('/reconcile') for path,_ in c.calls)
    assert result['budget']['reasoning_remaining_today'] is None


def test_audit_persists_each_page_and_resumes_without_claims(tmp_path):
    store=a.Store(tmp_path/'state')
    class Client:
        def __init__(self):self.calls=[]
        def call(self,method,path,payload=None):
            self.calls.append(path)
            assert method=='GET' and '/notes/audit?' in path
            if 'cursor=' not in path:
                return {'cutoff':'2026-09-09T10:00:00+00:00','next_cursor':'next',
                        'items':[{'id':'one','processing':{'state':'blocked','obligations':[]}}]}
            return {'cutoff':'2026-09-09T10:00:00+00:00','next_cursor':None,
                    'items':[{'id':'two','processing':{'state':'processed','obligations':[]}}]}
    c=Client()
    first=a.audit(c,store,{'audit_pages_per_pass':1})
    assert first['complete'] is False
    second=a.audit(c,store,{'audit_pages_per_pass':1})
    assert second['complete'] is True
    assert second['count']==2
    assert second['states']=={'blocked':1,'processed':1}
    assert len(c.calls)==2


def test_sensor_uses_its_packaged_adapter_and_explicit_state(tmp_path,monkeypatch):
    import head_of_sales_sensor as sensor
    import json
    token=tmp_path/'fixture-token';token.write_text('test-fixture-only');token.chmod(0o600)
    config=tmp_path/'config.json';state=tmp_path/'isolated-state'
    config.write_text(json.dumps({'enabled':True,'gmail_mailbox':sensor.MAILBOX,
        'token_file':str(token),'state_file':str(state),'crm_python':sys.executable}))
    calls=[]
    class Process:
        returncode=0
        def __init__(self,cmd,**kwargs):calls.append(cmd)
        def communicate(self,**kwargs):return ('{"wakeAgent":false}','')
    monkeypatch.setattr(sensor.subprocess,'Popen',Process)
    assert sensor.run_sensor(config)['wakeAgent'] is False
    assert calls[0][1]==str(ROOT/'head_of_sales_adapter.py')
    assert calls[0][calls[0].index('--state')+1]==str(state)


def test_note_adapter_never_creates_local_provider_objects(tmp_path):
    store=a.Store(tmp_path/'state')
    store.save_claim({'id':'w1','kind':'call_followup','lease_token':'lease','payload':{}})
    class Client:
        def __init__(self):self.posts=[]
        def call(self,method,path,payload=None):
            if method=='POST':
                self.posts.append(payload)
                return {'status':'completed','result':{'plan_hash':'verified'}}
            return {'id':'w1','status':'completed' if self.posts else 'running',
                    'result':{'plan_hash':'verified'},'context':{'lead_version':1,
                    'note_source':{'status':'current','source_digest':'a'*64,'summary':'Intro'}}}
    c=Client()
    value={'expected_lead_version':1,'source_digest':'a'*64,'facts':[], 'summary':'Intro',
           'actions':[{'key':'intro','task_type':'email','quote':'Intro',
                       'draft':{'recipient':'literal@example.test','subject':'Intro','body_html':'<p>Intro</p>'}}]}
    result=a.note_plan(c,store,'w1',value,{})
    assert result['status']=='completed'
    assert c.posts==[value|{'lease_token':'lease'}]
    assert not (tmp_path/'draft-journal').exists()


def test_adapter_rejects_model_receipt_before_any_post(tmp_path):
    store=a.Store(tmp_path/'state')
    store.save_claim({'id':'w1','kind':'call_followup','lease_token':'lease','payload':{}})
    class Client:
        def call(self,*args,**kwargs):raise AssertionError('No API or provider I/O for forged receipt')
    with pytest.raises(a.AdapterError):
        a.note_plan(Client(),store,'w1',{'actions':[{'draft_receipt':{'draft_id':'fake'}}]}, {})
