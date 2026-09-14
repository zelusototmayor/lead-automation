from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from tests.integration.api.test_agent_work_api import work_api, headers
from tests.integration.api.test_note_plan import prepare
from tests.integration.api.test_note_company_scope import assert_no_effects
from src.crm.persistence.models import Task


@pytest.mark.parametrize('task_type', ['call', 'follow_up', 'email'])
@pytest.mark.parametrize('offset', [-1, 0])
def test_due_at_must_be_future_before_any_plan_effect(work_api, monkeypatch, task_type, offset):
    from src.crm.services import note_plan_service as service
    client, engine, workspace, lead_id = work_api
    work_id, source_id, plan = prepare(work_api)
    calls = []
    monkeypatch.setattr(service, 'draft_for_action', lambda **kw: calls.append(kw))
    email = plan['actions'][0] | {'key': 'intro', 'task_type': 'email',
        'draft': {'recipient': 'ana@example.test', 'subject': 'Intro', 'body_html': '<p>Olá</p>'}}
    invalid = plan['actions'][0] | {'task_type': task_type,
        'due_at': (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()}
    if task_type == 'email':
        invalid['draft'] = email['draft']
        plan['actions'] = [invalid]
    else:
        # Even an earlier valid email intent must not reach the provider.
        plan['actions'] = [email, invalid]
    reply = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert reply.status_code == 409, reply.text
    assert calls == []
    assert_no_effects(engine, workspace, work_id, source_id)


def test_adopted_past_callback_cannot_hide_behind_future_plan_date(work_api):
    from uuid import uuid4
    client, engine, workspace, lead_id = work_api
    work_id, source_id, plan = prepare(work_api)
    with Session(engine) as session, session.begin():
        task = Task(workspace_id=workspace, lead_id=lead_id, task_type='call', title='Past callback',
            due_at=datetime.now(UTC)-timedelta(days=1), status='open', owner_user_id=uuid4())
        session.add(task)
        session.flush()
        task_id = task.id
        plan['actions'][0].update(existing_task_id=str(task.id), expected_task_version=task.version)
    reply = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert reply.status_code == 409, reply.text
    assert_no_effects(engine, workspace, work_id, source_id, [task_id])


def test_exact_future_callback_time_is_preserved(work_api):
    client, engine, workspace, lead_id = work_api
    work_id, _, plan = prepare(work_api)
    due = datetime.now(UTC).replace(hour=10, minute=37, second=0, microsecond=0) + timedelta(days=5)
    plan['actions'][0]['due_at'] = due.isoformat()
    reply = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert reply.status_code == 200, reply.text
    assert datetime.fromisoformat(reply.json()['result']['tasks'][0]['due_at']) == due
