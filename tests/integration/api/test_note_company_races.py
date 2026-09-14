"""Real PostgreSQL company fences, no live provider calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.crm.persistence.models import Account, Activity, AgentWork, Contact, Lead, Task
from tests.integration.api.test_agent_work_api import work_api, headers
from tests.integration.api.test_note_plan import prepare
from tests.integration.api.test_note_company_scope import sibling_lead, assert_no_effects


@pytest.mark.parametrize('target', ['account', 'sibling_lead', 'contact', 'task'])
def test_competing_manual_locks_fail_closed_without_provider_effect(work_api, monkeypatch, target):
    from src.crm.services import note_plan_service as service
    client, engine, workspace, lead_id = work_api
    work_id, source_id, plan = prepare(work_api)
    calls = []
    monkeypatch.setattr(service, 'draft_for_action', lambda **kw: calls.append(kw))
    plan['actions'][0].update(task_type='email', draft={
        'recipient': 'ana@example.test', 'subject': 'Intro', 'body_html': '<p>Olá</p>'})
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id)
        original = session.get(Lead, lead_id)
        ids = {'account': (Account, original.account_id), 'sibling_lead': (Lead, sibling.id),
               'contact': (Contact, original.contact_id)}
        task = Task(workspace_id=workspace, lead_id=lead_id, account_id=original.account_id,
            task_type='email', title='Manual draft task', due_at=datetime.now(UTC)+timedelta(days=1),
            status='open', owner_user_id=uuid4())
        session.add(task)
        session.flush()
        task_id = task.id
        ids['task'] = (Task, task_id)
        plan['actions'][0].update(existing_task_id=str(task_id), expected_task_version=task.version)
    model, key = ids[target]
    with Session(engine) as manual, manual.begin():
        manual.scalar(select(model).where(model.id == key).with_for_update())
        reply = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
        assert reply.status_code == 409, reply.text
        assert reply.json()['detail'] == 'Work conflict'
    assert calls == []
    assert_no_effects(engine, workspace, work_id, source_id, [task_id])


def test_sibling_note_race_and_restart_produce_only_one_obligation(work_api, monkeypatch):
    from src.crm.services import note_plan_service as service
    client, engine, workspace, lead_id = work_api
    old_work, old_source, old_plan = prepare(work_api)
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id)
        sibling.contact_email = 'ana@example.test'
        sibling_id = sibling.id
    work_id, source_id, plan = prepare((client, engine, workspace, sibling_id))
    for candidate in (old_plan, plan):
        candidate['actions'][0].update(task_type='email', draft={
            'recipient': 'ana@example.test', 'subject': 'Intro', 'body_html': '<p>Olá</p>'})
    entered, release = Event(), Event()
    calls = []
    def draft(**kw):
        calls.append(kw)
        entered.set()
        assert release.wait(5), 'test coordination timeout'
        return {'draft_id': 'fixture', 'message_id': 'fixture', 'sent': False, 'status': 'draft'}
    monkeypatch.setattr(service, 'draft_for_action', draft)
    uri = f'/api/v1/agent/work/{work_id}/note-plan'
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(client.post, uri, json=plan, headers=headers())
        try:
            assert entered.wait(5)
            loser = client.post(f'/api/v1/agent/work/{old_work}/note-plan', json=old_plan, headers=headers())
            assert loser.status_code == 409, loser.text
            assert loser.json()['detail'] == 'Work conflict'
        finally:
            release.set()
        assert winner.result(timeout=5).status_code == 200
    assert client.post(uri, json=plan, headers=headers()).json()['replayed'] is True
    assert client.post(f'/api/v1/agent/work/{old_work}/note-plan', json=old_plan, headers=headers()).status_code == 409
    assert len(calls) == 1
    with Session(engine) as session:
        assert len(list(session.scalars(select(Task).where(Task.workspace_id == workspace)))) == 1
        assert session.get(AgentWork, old_work).status == 'running'
        assert session.get(Activity, old_source).call_details is None


def test_accountless_source_ignores_other_accountless_refusal(work_api):
    client, engine, workspace, lead_id = work_api
    with Session(engine) as session, session.begin():
        original = session.get(Lead, lead_id)
        original.contact_id = None
        session.flush()
        original.account_id = None
    work_id, source_id, plan = prepare(work_api)
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id, accountless=True)
        session.add(Activity(workspace_id=workspace, lead_id=sibling.id, activity_type='note',
            title='Unrelated refusal', summary='Não contactar.', occurred_at=datetime.now(UTC),
            source_system='manual', actor_type='user'))
    reply = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert reply.status_code == 200, reply.text
