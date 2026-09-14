#!/usr/bin/env python3
"""Bounded Hermes/CRM adapter; no email sender and no model call on empty work.

CRM owns business state, leases and idempotency. Local SQLite stores claim receipts
and minimal execution metrics so Hermes can finish with a work ID, without putting
lease credentials in prompts. API errors expose only status/class, never bodies,
tokens or URLs. A failure never advances the server work item as completed.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

HERMES=Path.home()/'.hermes'
DEFAULT_CONFIG=HERMES/'ops/head-of-sales/config.json'
DEFAULT_STATE=HERMES/'ops/head-of-sales/state.sqlite3'
LISBON=ZoneInfo('Europe/Lisbon')

class AdapterError(Exception):pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise AdapterError('CRM redirect refused')

class Client:
    def __init__(self,base,token,timeout=10):
        u=urllib.parse.urlsplit(base)
        if not u.hostname or u.username or u.password or u.query or u.fragment:
            raise AdapterError('Invalid CRM base URL')
        if u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','::1')):
            raise AdapterError('CRM requires HTTPS or loopback HTTP')
        if not token:raise AdapterError('CRM credential unavailable')
        self.base=base.rstrip('/');self.token=token;self.timeout=timeout;self.requests=0
        self.opener=urllib.request.build_opener(NoRedirect)
    def call(self,method,path,payload=None):
        if not path.startswith('/api/v1/agent/'):raise AdapterError('Invalid API scope')
        self.requests+=1
        headers={'Authorization':'Bearer '+self.token,'X-Agent-Timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),'Accept':'application/json'}
        body=None
        if payload is not None:body=json.dumps(payload).encode();headers['Content-Type']='application/json'
        try:
            response=self.opener.open(urllib.request.Request(self.base+path,data=body,headers=headers,method=method),timeout=self.timeout)
            with response:
                raw=response.read(2_000_001)
                if len(raw)>2_000_000:raise AdapterError('CRM response exceeded budget')
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:raise AdapterError('CRM HTTP '+str(e.code)) from None
        except (urllib.error.URLError,TimeoutError):raise AdapterError('CRM transport unavailable') from None
        except ValueError:raise AdapterError('CRM response invalid') from None

class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.conn=sqlite3.connect(self.path,timeout=5);self.conn.row_factory=sqlite3.Row
        os.chmod(self.path,0o600)
        self.conn.executescript('''CREATE TABLE IF NOT EXISTS claims (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, attempt INTEGER NOT NULL,
          lease_token TEXT NOT NULL, lease_until TEXT, state TEXT NOT NULL,
          payload_hash TEXT NOT NULL, result_hash TEXT, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS counters (day TEXT,key TEXT,value INTEGER NOT NULL,PRIMARY KEY(day,key));
        CREATE TABLE IF NOT EXISTS source_checkpoints (scope TEXT PRIMARY KEY,since TEXT NOT NULL,cursor TEXT,scan_started TEXT NOT NULL,last_success TEXT);
        CREATE TABLE IF NOT EXISTS review_resolutions (resolution_id TEXT PRIMARY KEY,work_id TEXT NOT NULL,content_hash TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,ack TEXT,updated_at REAL NOT NULL);''')
    def count(self,key,delta=0):
        day=dt.datetime.now(LISBON).date().isoformat()
        if delta:
            with self.conn:self.conn.execute('INSERT INTO counters VALUES(?,?,?) ON CONFLICT(day,key) DO UPDATE SET value=value+excluded.value',(day,key,delta))
        row=self.conn.execute('SELECT value FROM counters WHERE day=? AND key=?',(day,key)).fetchone()
        return row[0] if row else 0
    def save_claim(self,item):
        work_id=str(item['id']);attempt=int(item.get('attempt') or 1);kind=str(item['kind']);lease=str(item['lease_token'])
        if not work_id or not lease:raise AdapterError('CRM claim missing identity')
        payload_hash=digest(item.get('payload',{}))
        old=self.get(work_id)
        same=old and old['lease_token']==lease and old['state'] in ('claimed','completed','waiting')
        if same:return False
        with self.conn:self.conn.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,attempt=excluded.attempt,lease_token=excluded.lease_token,lease_until=excluded.lease_until,state=excluded.state,payload_hash=excluded.payload_hash,result_hash=NULL,updated_at=excluded.updated_at',(work_id,kind,attempt,lease,item.get('lease_until'),'claimed',payload_hash,None,time.time()))
        return not same
    def get(self,key):return self.conn.execute('SELECT * FROM claims WHERE id=?',(key,)).fetchone()
    def finished(self,key,state,result):
        with self.conn:self.conn.execute('UPDATE claims SET state=?,result_hash=?,updated_at=? WHERE id=?',(state,digest(result),time.time(),key))
    def close(self):self.conn.close()

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def safe_id(value):
    value=str(value)
    if not value or len(value)>200 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):raise AdapterError('Invalid work ID')
    return value

def compact_item(item):
    return {key:item[key] for key in ('id','kind','task_id','lead_id','payload','context','attempt','lease_until') if key in item}

def source_checkpoint(store,scope,initial_since,now=None):
    row=store.conn.execute('SELECT * FROM source_checkpoints WHERE scope=?',(scope,)).fetchone()
    if row:return dict(row)
    now=now or dt.datetime.now(dt.timezone.utc).isoformat()
    value={'scope':scope,'since':initial_since,'cursor':None,'scan_started':now,'last_success':None}
    with store.conn:store.conn.execute('INSERT INTO source_checkpoints VALUES(?,?,?,?,?)',tuple(value.values()))
    return value


def accept_page(client,store,checkpoint,page):
    observations=page.get('observations')
    if not isinstance(observations,list) or len(observations)>20:raise AdapterError('Gmail page exceeded observation contract')
    if any(o.get('source_scope')!=checkpoint['scope'] for o in observations):raise AdapterError('Gmail observation scope mismatch')
    if observations:
        receipt=client.call('POST','/api/v1/agent/providers/observations',{'observations':observations})
        if int(receipt.get('accepted',-1))+int(receipt.get('duplicates',-1))!=len(observations) or len(receipt.get('items',[]))!=len(observations):raise AdapterError('CRM did not confirm complete observation batch')
    cursor=page.get('next_cursor')
    since=checkpoint['since'];started=checkpoint['scan_started'];now=dt.datetime.now(dt.timezone.utc)
    if not cursor:
        # Revisit an overlap; idempotent ingestion prevents double commercial work.
        since=(dt.datetime.fromisoformat(started.replace('Z','+00:00'))-dt.timedelta(minutes=5)).isoformat()
        started=now.isoformat()
    with store.conn:store.conn.execute('UPDATE source_checkpoints SET since=?,cursor=?,scan_started=?,last_success=? WHERE scope=?',(since,cursor,started,now.isoformat(),checkpoint['scope']))
    store.count('observations',len(observations))
    return {'observations':len(observations),'has_more':bool(cursor),'scanned_since':checkpoint['since']}


def sync_local(client,store,config):
    mailbox=config.get('gmail_mailbox');credentials=config.get('gmail_credentials_file')
    if mailbox!='zelu@zelusottomayor.com':raise AdapterError('Only zelu mailbox may feed commercial CRM observations')
    if not mailbox or not credentials or not config.get('crm_code_root') or not config.get('crm_python'):raise AdapterError('Local Gmail collector configuration incomplete')
    initial=config.get('gmail_initial_since')
    if not initial:raise AdapterError('Explicit Gmail initial coverage required')
    try:
        parsed=dt.datetime.fromisoformat(initial.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError()
    except (ValueError,AttributeError):raise AdapterError('Initial coverage requires timezone') from None
    checkpoint=source_checkpoint(store,'mailbox:'+mailbox,initial)
    payload={'code_root':config['crm_code_root'],'credentials_file':credentials,'mailbox':mailbox,'since':checkpoint['since'],'cursor':checkpoint['cursor'],'limit':min(20,int(config.get('max_sync_items',20)))}
    try:
        run=subprocess.run([config['crm_python'],str(Path(__file__).with_name('head_of_sales_google_page.py'))],input=json.dumps(payload),text=True,capture_output=True,timeout=min(35,int(config.get('google_page_timeout_seconds',30))))
    except (OSError,subprocess.TimeoutExpired):raise AdapterError('Gmail page unavailable or timed out; cursor unchanged') from None
    if run.returncode or len(run.stdout)>1_000_000:raise AdapterError('Gmail page unavailable; cursor unchanged')
    try:page=json.loads(run.stdout)
    except ValueError:raise AdapterError('Gmail page invalid; cursor unchanged') from None
    return accept_page(client,store,checkpoint,page)


REASONING_KINDS=['call_followup','reply_review','proposal_review','identity_review','sent_review','calendar_review','followup_due']


def sync(client,store,config):
    """Ingest source evidence only. EOD must never claim the Sales worker queue."""
    mode=config.get('sync_mode','server' if config.get('sync_google',True) else 'disabled')
    try:
        if mode=='local':coverage=sync_local(client,store,config)
        elif mode=='server':coverage=client.call('POST','/api/v1/agent/providers/google/sync',{'limit':min(20,int(config.get('max_sync_items',20)))})
        else:coverage={'status':'provider_sync_disabled'}
    except AdapterError as error:
        coverage={'status':'retryable_failure','reason':str(error)}
        store.count('transport_failures',1)
    return {'wakeAgent':False,'coverage':coverage}


def collect(client,store,config):
    started=time.monotonic();max_seconds=min(50,int(config.get('max_run_seconds',45)))
    budget=min(3,max(1,int(config.get('max_work_per_run',2))))
    deterministic=min(3,max(1,int(config.get('max_deterministic_work_per_run',3))))
    daily=min(100,max(1,int(config.get('max_reasoning_tasks_per_day',24))))
    hourly=config.get('hourly_drain') is True
    remaining=budget if hourly else max(0,daily-store.count('reasoning_claims'))
    now=dt.datetime.now(LISBON)
    reasoning_allowed=(config.get('reasoning_business_hours', True) is False or (now.weekday()<5 and 10<=now.hour<=19))
    if not reasoning_allowed:remaining=0
    result={'wakeAgent':False,'items':[],'automatic_completed':0,'automatic_failures':0,'waiting':0,'coverage':{'status':'not_scanned_this_run'},'budget':{'max_work_per_run':budget,'max_deterministic_work_per_run':deterministic,'reasoning_remaining_today':None if hourly else remaining},'policy_path':str(HERMES/'ops/operating-policy.json'),'prompt_path':str(HERMES/'prompts/head-of-sales.md')}
    if hourly:
        result['note_coverage']=client.call('POST','/api/v1/agent/notes/reconcile',{'limit':50})
    worker=str(config.get('worker_id','hermes-head-of-sales-mac-mini'))
    lease_seconds=600  # exceeds the bounded model worker's complete lifetime
    # Existing Calendar obligations have their own deterministic allowance and
    # run before Gmail collection or the model budget check.
    claims=client.call('POST','/api/v1/agent/work/claim',{'worker_id':worker,'limit':deterministic,'lease_seconds':lease_seconds,'kinds':['calendar_callback']})
    items=claims.get('items',[])
    if not isinstance(items,list) or len(items)>deterministic:raise AdapterError('CRM deterministic claim batch exceeded contract')
    for item in items:
        safe_id(item['id'])
        if item.get('kind')!='calendar_callback':raise AdapterError('CRM returned work outside requested kind scope')
        if not store.save_claim(item):store.count('duplicate_claims',1);continue
        store.count('claims',1)
        if time.monotonic()-started>=max_seconds:break  # lease remains recoverable
        try:
            reply=client.call('POST','/api/v1/agent/work/'+safe_id(item['id'])+'/execute',{'lease_token':item['lease_token']})
            state=reply.get('status') or (reply.get('item') or {}).get('status')
            if state not in ('completed','waiting') or (state=='completed' and not (reply.get('result') or {}).get('evidence')):
                raise AdapterError('CRM did not confirm Calendar result with evidence')
            store.finished(item['id'],state,reply);store.count(state,1)
            result['automatic_completed' if state=='completed' else 'waiting']+=1
        except AdapterError:
            store.count('transport_failures',1);result['automatic_failures']+=1
    if time.monotonic()-started<max_seconds:
        # A provider failure cannot hide confirmed callbacks or starve queued work.
        result['coverage']=sync(client,store,config)['coverage']
    if not remaining:
        result['budget']['reasoning_exhausted']=True;store.count('budget_limited_runs',1)
    elif time.monotonic()-started<max_seconds:
        claims=client.call('POST','/api/v1/agent/work/claim',{'worker_id':worker,'limit':min(budget,remaining),'lease_seconds':lease_seconds,'kinds':REASONING_KINDS})
        items=claims.get('items',[])
        if not isinstance(items,list) or len(items)>budget:raise AdapterError('CRM claim batch exceeded contract')
        for item in items:
            safe_id(item['id'])
            if item.get('kind') not in REASONING_KINDS:raise AdapterError('CRM returned work outside reasoning scope')
            if not store.save_claim(item):store.count('duplicate_claims',1);continue
            store.count('claims',1);store.count('reasoning_claims',1);result['items'].append(compact_item(item))
    if hourly and not result['items']:
        result['audit']=audit(client,store,config) if time.monotonic()-started<max_seconds-20 else {'complete':False,'reason':'duration_budget'}
    result['wakeAgent']=bool(result['items']);store.count('api_requests',client.requests)
    return result

def audit(client,store,config):
    """Read every obligation page, durable resume; never claim or create work."""
    from collections import Counter
    store.conn.execute('CREATE TABLE IF NOT EXISTS note_audit (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
    row=store.conn.execute('SELECT data FROM note_audit WHERE id=1').fetchone()
    data=json.loads(row[0]) if row else {}
    if data.get('complete') or not data:
        data={'cursor':None,'cutoff':None,'items':{},'complete':False}
    for _ in range(max(1,min(3,int(config.get('audit_pages_per_pass',2))))):
        params={'limit':1}
        if data['cursor']:params.update(cursor=data['cursor'],cutoff=data['cutoff'])
        page=client.call('GET','/api/v1/agent/notes/audit?'+urllib.parse.urlencode(params))
        if not isinstance(page.get('items'),list) or 'next_cursor' not in page or not page.get('cutoff'):
            raise AdapterError('Incomplete note audit page; checkpoint unchanged')
        if data['cutoff'] and page['cutoff']!=data['cutoff']:
            raise AdapterError('Audit coverage changed; checkpoint unchanged')
        for item in page['items']:
            data['items'][item['id']]={'id':item['id'],'processing':item['processing']}
        previous=data['cursor'];data.update(cursor=page['next_cursor'],cutoff=page['cutoff'],complete=page['next_cursor'] is None)
        if data['cursor'] and data['cursor']==previous:
            raise AdapterError('Audit cursor did not advance')
        with store.conn:store.conn.execute('INSERT INTO note_audit VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(json.dumps(data),))
        if data['complete']:break
    counts=dict(Counter(x['processing']['state'] for x in data['items'].values()))
    return {'complete':data['complete'],'count':len(data['items']),'states':counts,
        'cutoff':data['cutoff'],'next_cursor':data['cursor'],
        'scope':'per-obligation CRM/Calendar receipts and fresh draft MIME; no send claim','receipt_store':str(store.path)}


def validate_result(value):
    if not isinstance(value,dict) or value.get('status') not in ('completed','waiting'):raise AdapterError('Result status must be completed or waiting')
    result=value.get('result')
    if not isinstance(result,dict) or not isinstance(result.get('summary'),str) or not result['summary'].strip():raise AdapterError('Result requires summary')
    if len(result['summary'])>2000:raise AdapterError('Result summary exceeded budget')
    if not isinstance(result.get('evidence'),list) or any(not isinstance(e,dict) or not e.get('provider') for e in result.get('evidence',[])):raise AdapterError('Evidence requires objects with provider and source reference')
    if len(json.dumps(value).encode())>16384:raise AdapterError('Result exceeded evidence budget')
    if value['status']=='completed' and not result['evidence']:raise AdapterError('Completion requires source evidence')
    next_action=result.get('next_action')
    if not isinstance(next_action,dict):raise AdapterError('Result requires next_action or explicit terminal state')
    if not next_action.get('terminal') and not (next_action.get('owner') and (next_action.get('action') or next_action.get('waiting_reason'))):raise AdapterError('Next step requires owner and action/waiting reason')
    if value['status']=='waiting' and not next_action.get('waiting_reason'):raise AdapterError('Waiting requires exact reason')
    if result.get('external_send') or result.get('new_price') or result.get('contract_change'):raise AdapterError('External commercial mutation is outside this adapter')
    return value

def finish(client,store,key,value):
    key=safe_id(key);value=validate_result(value);claim=store.get(key)
    if not claim:raise AdapterError('No local claim for this work ID')
    if claim['state'] in ('completed','waiting'):
        if claim['result_hash']==digest(value):return {'id':key,'status':claim['state'],'duplicate':True}
        raise AdapterError('Work already finished with a different result')
    response=client.call('POST','/api/v1/agent/work/'+key+'/finish',{'lease_token':claim['lease_token'],**value})
    remote_status=response.get('status') or (response.get('item') or {}).get('status')
    if remote_status!=value['status']:raise AdapterError('CRM did not confirm requested final state')
    store.finished(key,value['status'],value);store.count(value['status'],1)
    return {'id':key,'status':value['status'],'verified_by':'CRM agent API'}

def next_action(client,store,key,value):
    key=safe_id(key);claim=store.get(key)
    if not claim or claim['state']!='claimed':raise AdapterError('No active local claim for this work ID')
    allowed={'expected_lead_version','title','due_at','task_type','supersede_agent_followups'}
    if not isinstance(value,dict) or set(value)-allowed:raise AdapterError('Invalid next-action fields')
    if value.get('task_type') not in ('email','follow_up'):raise AdapterError('Only internal email/followup tasks are allowed')
    if not isinstance(value.get('expected_lead_version'),int) or value['expected_lead_version']<1:raise AdapterError('Current lead version required')
    if not isinstance(value.get('title'),str) or not value['title'].strip() or len(value['title'])>300:raise AdapterError('Concrete next action required')
    if not value.get('due_at'):raise AdapterError('Grounded due date required')
    value=dict(value);value['supersede_agent_followups']=bool(value.get('supersede_agent_followups',False))
    response=client.call('POST','/api/v1/agent/work/'+key+'/next-action',{'lease_token':claim['lease_token'],**value})
    if response.get('status')!='completed':raise AdapterError('CRM did not confirm next action')
    store.finished(key,'completed',response);store.count('next_actions',1);store.count('completed',1)
    return response


def resolve(client,store,key,resolution_id,result):
    """Close only an evidenced waiting review; no business entity is mutated.

    Journal the original server result hash before POST. A retry after an ACK
    was lost must use that exact command, not a newly fetched concurrency hash.
    """
    key=safe_id(key)
    try:resolution_id=str(uuid.UUID(str(resolution_id)))
    except (ValueError,AttributeError):raise AdapterError('Explicit resolution UUID required') from None
    if not isinstance(result,dict) or set(result)!={'summary','evidence'}:raise AdapterError('Review resolution allows only summary and evidence, no next action')
    if not isinstance(result['summary'],str) or not result['summary'].strip() or len(result['summary'])>2000:raise AdapterError('Review resolution requires a bounded summary')
    evidence=result['evidence']
    if not isinstance(evidence,list) or not 1<=len(evidence)<=20 or any(not isinstance(e,dict) or not e.get('provider') for e in evidence):raise AdapterError('Review resolution requires referenced source evidence')
    if len(json.dumps({'status':'completed','result':result}).encode())>16000:raise AdapterError('Review evidence exceeded budget')
    content_hash=digest(result)
    old=store.conn.execute('SELECT * FROM review_resolutions WHERE resolution_id=?',(resolution_id,)).fetchone()
    if old:
        if old['work_id']!=key or old['content_hash']!=content_hash:raise AdapterError('Resolution UUID already belongs to a different command')
        if old['state']=='confirmed':return {**json.loads(old['ack']),'replayed_local':True}
        payload=json.loads(old['payload'])
    else:
        current=client.call('GET','/api/v1/agent/work/'+key)
        if str(current.get('id'))!=key or current.get('status')!='waiting' or current.get('kind') not in ('reply_review','proposal_review','identity_review','sent_review'):raise AdapterError('Only a current waiting review can be resolved')
        expected=current.get('result_hash')
        if not isinstance(expected,str) or len(expected)!=64 or any(c not in '0123456789abcdefABCDEF' for c in expected):raise AdapterError('Current CRM review fingerprint unavailable')
        previous={digest(e) for e in (current.get('result') or {}).get('evidence',[])}
        if not any(digest(e) not in previous for e in evidence):raise AdapterError('Resolution requires new source evidence')
        payload={'resolution_id':resolution_id,'expected_result_hash':expected,'result':result}
        with store.conn:store.conn.execute('INSERT INTO review_resolutions VALUES(?,?,?,?,?,?,?)',(resolution_id,key,content_hash,json.dumps(payload,ensure_ascii=False),'pending',None,time.time()))
    response=client.call('POST','/api/v1/agent/work/'+key+'/resolve',payload)
    if str(response.get('id'))!=key or response.get('status')!='completed' or str(response.get('resolution_id'))!=resolution_id or response.get('result')!=result:raise AdapterError('CRM did not confirm the exact review resolution')
    receipt={'id':key,'status':'completed','resolution_id':resolution_id,'review_only':True,'verified_by':'CRM agent API','replayed':bool(response.get('replayed'))}
    # Completion is recorded only after the exact authenticated ACK. Keep a
    # resolution receipt even for work that was originally handled elsewhere.
    with store.conn:
        store.conn.execute('UPDATE review_resolutions SET state=?,ack=?,updated_at=? WHERE resolution_id=?',('confirmed',json.dumps(receipt),time.time(),resolution_id))
        store.conn.execute('UPDATE claims SET state=?,result_hash=?,updated_at=? WHERE id=?',('completed',digest({'status':'completed','result':result}),time.time(),key))
    store.count('review_resolutions',1)
    return receipt


def note_plan(client,store,key,value,config):
    """Forward intent only; canonical server owns provider effects and fences."""
    key=safe_id(key);claim=store.get(key)
    if not claim:raise AdapterError('No owned CRM claim')
    if not isinstance(value,dict) or set(value)-{'expected_lead_version','source_digest','facts','actions','summary','disposition'}:
        raise AdapterError('Invalid note plan')
    if any(not isinstance(x,dict) or 'draft_receipt' in x for x in value.get('actions',[])):
        raise AdapterError('Provider receipt cannot be supplied by model')
    payload={**value,'lease_token':claim['lease_token']}
    # Lost ACK retries the identical server-fenced command, never another POST
    # to Gmail or an unconditional locally-completed shortcut.
    reply=client.call('POST','/api/v1/agent/work/'+key+'/note-plan',payload)
    readback=client.call('GET','/api/v1/agent/work/'+key)
    if reply.get('status')!='completed' or readback.get('status')!='completed' or readback.get('result')!=reply.get('result'):
        raise AdapterError('CRM note plan readback failed')
    store.finished(key,'completed',reply);store.count('completed',1)
    return {'id':key,'status':'completed','result':readback['result'],'verified_by':'CRM exact readback'}


@contextlib.contextmanager
def locked(path):
    path=Path(str(path)+'.lock');path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a') as f:
        os.chmod(path,0o600)
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise AdapterError('Adapter already running') from None
        yield

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,default=DEFAULT_CONFIG);p.add_argument('--state',type=Path,default=DEFAULT_STATE)
    sub=p.add_subparsers(dest='command',required=True);sub.add_parser('collect');sub.add_parser('sync');sub.add_parser('status');f=sub.add_parser('finish');f.add_argument('--id',required=True);f.add_argument('--result-file',type=Path,required=True)
    sub.add_parser('audit')
    q=sub.add_parser('queue');q.add_argument('--status',choices=('queued','running','waiting','completed','failed'))
    n=sub.add_parser('note-plan');n.add_argument('--id',required=True);n.add_argument('--plan-file',type=Path,required=True)
    n=sub.add_parser('next-action');n.add_argument('--id',required=True);n.add_argument('--action-file',type=Path,required=True)
    r=sub.add_parser('resolve');r.add_argument('--id',required=True);r.add_argument('--request-id',required=True);r.add_argument('--result-file',type=Path,required=True)
    a=p.parse_args()
    try:
        config=json.loads(a.config.read_text())
        if not config.get('enabled'):
            print(json.dumps({'wakeAgent':False,'state':'not_activated','reason':'CRM deployment configuration pending'}));return 0
        with locked(a.state):
            store=Store(a.state)
            try:
                if a.command=='status':
                    print(json.dumps({'enabled':True,'counts':{k:store.count(k) for k in ('claims','completed','waiting','reasoning_claims','duplicate_claims','transport_failures','api_requests')}}));return 0
                token=os.environ.get('CRM_AUTOMATION_BEARER_TOKEN','')
                if not token:
                    token_path=Path(os.environ.get('CRM_AUTOMATION_BEARER_TOKEN_FILE') or config.get('token_file') or HERMES/'ops/head-of-sales/automation-token')
                    info=token_path.stat()
                    if info.st_uid!=os.getuid() or info.st_mode & 0o077:raise AdapterError('Automation credential must remain private')
                    token=token_path.read_text().strip()
                base=os.environ.get('CRM_AGENT_BASE_URL') or config.get('base_url','')
                client=Client(base,token,timeout=min(20,int(config.get('request_timeout_seconds',10))))
                if a.command=='collect':out=collect(client,store,config)
                elif a.command=='audit':out={'wakeAgent':False,'audit':audit(client,store,config)}
                elif a.command=='sync':
                    out=sync(client,store,config);store.count('api_requests',client.requests)
                elif a.command=='queue':
                    path='/api/v1/agent/work?limit=20'+('&status='+a.status if a.status else '')
                    remote=client.call('GET',path)
                    out={'scope':'bounded CRM work sample; not complete pipeline or sales metrics','limit':20,'items':[{k:v for k,v in item.items() if k in ('id','kind','status','task_id','lead_id','payload','attempt','available_at','result','updated_at')} for item in remote.get('items',[])[:20]]}
                elif a.command=='note-plan':out=note_plan(client,store,a.id,json.loads(a.plan_file.read_text()),config)
                elif a.command=='next-action':out=next_action(client,store,a.id,json.loads(a.action_file.read_text()))
                elif a.command=='resolve':out=resolve(client,store,a.id,a.request_id,json.loads(a.result_file.read_text()))
                else:out=finish(client,store,a.id,json.loads(a.result_file.read_text()))
                print(json.dumps(out,ensure_ascii=False));return 0
            finally:store.close()
    except (AdapterError,OSError,ValueError,KeyError) as e:
        # Do not stringify unknown exceptions: credentials/URLs can be embedded.
        reason=str(e) if isinstance(e,AdapterError) else 'Adapter configuration or local state unavailable'
        print(json.dumps({'wakeAgent':False,'state':'retryable_failure','reason':reason}));return 1

if __name__=='__main__':raise SystemExit(main())
