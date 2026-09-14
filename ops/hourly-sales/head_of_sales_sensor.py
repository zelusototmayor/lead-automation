#!/usr/bin/env python3
"""Silent scheduler wrapper for the bounded local Head of Sales adapter.

Enables nothing by itself. The deployment owner installs the scoped service token
and explicitly enables configuration only after the CRM canary. Only the commercial
zelu mailbox may feed canonical CRM writes; other inboxes remain context sensors.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys

HERMES=Path.home()/'.hermes'
CONFIG=HERMES/'ops/head-of-sales/config.json'
ADAPTER=Path(__file__).with_name('head_of_sales_adapter.py')
MAILBOX='zelu@zelusottomayor.com'


def command_environment(config):
    if config.get('gmail_mailbox')!=MAILBOX:
        raise ValueError('Only the configured zelu commercial mailbox may write CRM observations')
    env=os.environ.copy()
    if not env.get('CRM_AUTOMATION_BEARER_TOKEN'):
        path=Path(env.get('CRM_AUTOMATION_BEARER_TOKEN_FILE') or config.get('token_file') or HERMES/'ops/head-of-sales/automation-token')
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise ValueError('Automation credential must be a private user-owned regular file')
        env['CRM_AUTOMATION_BEARER_TOKEN_FILE']=str(path)
    if config.get('base_url'):env['CRM_AGENT_BASE_URL']=config['base_url']
    return env


def run_sensor(config_path=CONFIG):
    cfg=json.loads(Path(config_path).read_text())
    if not cfg.get('enabled'):
        return {'wakeAgent':False,'state':'not_activated'}
    env=command_environment(cfg)
    runtime=cfg.get('crm_python') or sys.executable
    timeout=max(20,min(60,int(cfg.get('sensor_timeout_seconds',55))))
    state=cfg.get('state_file') or HERMES/'ops/head-of-sales/state.sqlite3'
    process=subprocess.Popen([runtime,str(ADAPTER),'--config',str(config_path),'--state',str(state),'collect'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,start_new_session=True)
    try:
        stdout,_=process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Include the read-only Gmail helper in the stop; server work remains
        # fenced/recoverable by lease expiry and observation cursors stay atomic.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.communicate()
        return {'wakeAgent':False,'state':'retryable_failure','reason':'Bounded sensor runtime exceeded; queue remains recoverable'}
    if len(stdout)>256000:
        return {'wakeAgent':False,'state':'retryable_failure','reason':'Sensor output exceeded budget'}
    try:result=json.loads(stdout)
    except ValueError:
        return {'wakeAgent':False,'state':'retryable_failure','reason':'Sensor result unavailable'}
    if not isinstance(result,dict) or type(result.get('wakeAgent')) is not bool:
        return {'wakeAgent':False,'state':'retryable_failure','reason':'Sensor contract invalid'}
    if process.returncode:
        return {'wakeAgent':False,'state':'retryable_failure','reason':result.get('reason','CRM sensor unavailable')}
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,default=CONFIG);a=p.parse_args()
    try:out=run_sensor(a.config)
    except (OSError,ValueError):out={'wakeAgent':False,'state':'retryable_failure','reason':'Sensor deployment configuration unavailable'}
    print(json.dumps(out,ensure_ascii=False))
    return 1 if out.get('state')=='retryable_failure' else 0

if __name__=='__main__':raise SystemExit(main())
