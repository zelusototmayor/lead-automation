"""Bounded hourly drain, real receipt verification (no commercial effects)."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'ops/hourly-sales'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('hourly_worker', ROOT / 'head_of_sales_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def test_cli_does_not_skip_deterministic_callbacks_outside_window(tmp_path,monkeypatch):
    config=tmp_path/'config.json';config.write_text(json.dumps({'enabled':True,'hourly_drain':True}))
    monkeypatch.setattr(sys,'argv',['worker','--config',str(config)])
    monkeypatch.setattr(worker,'inside_business_window',lambda now:False)
    calls=[];monkeypatch.setattr(worker,'execute',lambda path:calls.append(path) or [])
    assert worker.main()==0
    assert calls==[config]


def test_hourly_drains_more_than_two_and_preserves_receipts(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'enabled': True, 'hourly_drain': True, 'max_drain_seconds': 900}))
    batches = [
        {'wakeAgent': True, 'items': [{'id': str(i)}, {'id': str(i+1)}], 'coverage': {'status': 'ok'}}
        for i in (0, 2, 4)
    ] + [{'wakeAgent': False, 'items': [], 'coverage': {'status': 'ok'},'audit':{'complete':True,'states':{}}}]
    called = []
    def sensor(path):
        called.append(path)
        return batches.pop(0)
    monkeypatch.setattr(worker, 'run_sensor', sensor)
    monkeypatch.setattr(worker, 'run_model', lambda *a, **k: {'ok': True, 'contract': {'notify_jose': False, 'summary': ''}})
    monkeypatch.setattr(worker, 'receipts', lambda items,**kwargs: {item['id']: 'completed' for item in items})
    assert worker.execute(config, tmp_path / 'ops') == []
    assert len(called) == 4
    last = json.loads((tmp_path/'ops/last-run.json').read_text())
    assert last['processed'] == 6
    assert last['drained'] is True
    assert len(last['receipts']) == 6


def test_minimum_budget_still_scans_once(tmp_path,monkeypatch):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'enabled':True,'hourly_drain':True,'max_drain_seconds':240}))
    calls=[]
    def sensor(path):
        calls.append(path)
        return {'wakeAgent':False,'items':[],'coverage':{'status':'ok'},'audit':{'complete':True,'states':{}}}
    monkeypatch.setattr(worker,'run_sensor',sensor)
    worker.execute(config,tmp_path/'ops')
    assert len(calls)==1


def test_reconciliation_pages_are_drained_even_without_model_items(tmp_path,monkeypatch):
    config=tmp_path/'config.json';config.write_text(json.dumps({'enabled':True,'hourly_drain':True}))
    batches=[{'wakeAgent':False,'note_coverage':{'has_more':True}},
             {'wakeAgent':False,'note_coverage':{'has_more':False},'audit':{'complete':True,'states':{}}}]
    monkeypatch.setattr(worker,'run_sensor',lambda path:batches.pop(0))
    worker.execute(config,tmp_path/'ops')
    assert batches==[]
    assert json.loads((tmp_path/'ops/last-run.json').read_text())['drained'] is True


def test_model_query_includes_canonical_note_contract(tmp_path,monkeypatch):
    (tmp_path/'ops').mkdir();(tmp_path/'prompts').mkdir()
    (tmp_path/'ops/operating-policy.json').write_text('{}')
    (tmp_path/'prompts/head-of-sales.md').write_text('Existing Sales authority')
    monkeypatch.setattr(worker,'HERMES',tmp_path)
    query=worker.make_query({'items':[]})
    assert 'note-plan --id' in query
    assert 'source_digest' in query
    assert 'draft_receipt' in query
    assert 'Existing Sales authority' in query


def test_quiet_without_audit_is_not_falsely_drained(tmp_path,monkeypatch):
    config=tmp_path/'config.json';config.write_text(json.dumps({'enabled':True,'hourly_drain':True}))
    monkeypatch.setattr(worker,'run_sensor',lambda path:{'wakeAgent':False})
    worker.execute(config,tmp_path/'ops')
    assert json.loads((tmp_path/'ops/last-run.json').read_text())['drained'] is False


def test_lisbon_hours_use_dst_and_business_days():
    from datetime import datetime
    cases={'2026-09-09T09:00:00+00:00':True,
           '2026-09-09T08:59:00+00:00':False,
           '2026-01-09T09:00:00+00:00':False,
           '2026-01-09T10:00:00+00:00':True,
           '2026-09-12T10:00:00+00:00':False,
           '2026-09-09T18:00:00+00:00':True,
           '2026-09-09T19:00:00+00:00':False}
    assert {stamp:worker.inside_business_window(datetime.fromisoformat(stamp)) for stamp in cases}==cases


def test_incomplete_batch_stops_and_is_not_reported_drained(tmp_path, monkeypatch):
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'enabled': True, 'hourly_drain': True}))
    calls = []
    def sensor(path):
        calls.append(path)
        return {'wakeAgent': True, 'items': [{'id': 'unfinished'}], 'coverage': {'status': 'ok'}}
    monkeypatch.setattr(worker, 'run_sensor', sensor)
    monkeypatch.setattr(worker, 'run_model', lambda *a, **k: {'ok': True, 'contract': {}})
    monkeypatch.setattr(worker, 'receipts', lambda items,**kwargs: {'unfinished': 'claimed'})
    worker.execute(config, tmp_path/'ops')
    last=json.loads((tmp_path/'ops/last-run.json').read_text())
    assert len(calls)==1
    assert last['drained'] is False
    assert last['state']=='recoverable_failure'
