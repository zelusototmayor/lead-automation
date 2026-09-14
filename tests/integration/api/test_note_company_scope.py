"""Round-1 reviewer regressions: canonical company obligations, not lead-local."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.integration.api.test_agent_work_api import work_api, headers
from tests.integration.api.test_note_plan import prepare
from src.crm.persistence.models import Activity, AgentWork, AuditEvent, Lead, Task


def sibling_lead(session, workspace, original_id, *, accountless=False):
    original = session.get(Lead, original_id)
    sibling = Lead(workspace_id=workspace, account_id=None if accountless else original.account_id,
                   company_name=original.company_name)
    session.add(sibling)
    session.flush()
    return sibling


def assert_no_effects(engine, workspace, work_id, source_id, task_ids=()):
    with Session(engine) as session:
        assert set(session.scalars(select(Task.id).where(Task.workspace_id == workspace))) == set(task_ids)
        assert not list(session.scalars(select(AgentWork).where(
            AgentWork.workspace_id == workspace, AgentWork.kind == 'calendar_callback')))
        assert not list(session.scalars(select(AuditEvent).where(
            AuditEvent.workspace_id == workspace, AuditEvent.action == 'agent.note_interpreted')))
        assert session.get(AgentWork, work_id).status == 'running'
        assert session.get(Activity, source_id).call_details is None


@pytest.mark.parametrize('task_type', ['call', 'email', 'follow_up'])
@pytest.mark.parametrize('status', ['open', 'completed', 'cancelled'])
@pytest.mark.parametrize('account_on_task', [True, False])
def test_sibling_manual_tasks_block_duplicate_even_without_denormalized_account(
        work_api, monkeypatch, task_type, status, account_on_task):
    from src.crm.services import note_plan_service as service
    client, engine, workspace, lead_id = work_api
    work_id, source_id, plan = prepare(work_api)
    creates = []
    monkeypatch.setattr(service, 'draft_for_action', lambda **kw: creates.append(kw))
    action = plan['actions'][0]
    action['task_type'] = task_type
    if task_type == 'email':
        action['draft'] = {'recipient': 'ana@example.test', 'subject': 'Intro', 'body_html': '<p>Olá</p>'}
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id)
        completed = Activity(workspace_id=workspace, lead_id=sibling.id,
            account_id=sibling.account_id, activity_type='task', title='Manual task disposition',
            occurred_at=datetime.now(UTC), source_system='manual', actor_type='user')
        session.add(completed)
        session.flush()
        task = Task(workspace_id=workspace, lead_id=sibling.id,
            account_id=sibling.account_id if account_on_task else None,
            task_type=task_type, title='Existing manual obligation',
            due_at=datetime.now(UTC) + timedelta(days=1), owner_user_id=uuid4(), status=status,
            # Old creation must not hide a recently completed/cancelled task.
            created_at=datetime.now(UTC) - timedelta(days=2), updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC) if status == 'completed' else None,
            completion_activity_id=completed.id if status == 'completed' else None)
        session.add(task)
        session.flush()
        task_id = task.id
    response = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert response.status_code == 409, response.text
    assert creates == []
    assert_no_effects(engine, workspace, work_id, source_id, [task_id])


def test_accountless_lead_is_not_deduped_by_name_or_null_account(work_api):
    client, engine, workspace, lead_id = work_api
    with Session(engine) as session, session.begin():
        session.get(Lead, lead_id).contact_id = None
        session.flush()
        session.get(Lead, lead_id).account_id = None
    work_id, _, plan = prepare(work_api)
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id, accountless=True)
        session.add(Task(workspace_id=workspace, lead_id=sibling.id,
            task_type='call', title='Unrelated same-name callback', status='open',
            due_at=datetime.now(UTC) + timedelta(days=1), owner_user_id=uuid4()))
    response = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('kind,summary', [
    ('note', 'Não contactar. Recusa explícita.'),
    ('note', 'Aguardar validação do telefone. Não contactar.'),
    ('email_sent', 'Apresentação enviada por José.'),
    ('email_received', 'Não queremos mais contactos.'),
    ('call', 'Cliente pediu pausa.'),
    ('meeting', 'Recusa comunicada na reunião.'),
])
@pytest.mark.parametrize('lead_on_activity', [True, False])
def test_later_company_context_blocks_old_actions(work_api, monkeypatch, kind, summary, lead_on_activity):
    from src.crm.services import note_plan_service as service
    client, engine, workspace, lead_id = work_api
    work_id, source_id, plan = prepare(work_api)
    creates = []
    monkeypatch.setattr(service, 'draft_for_action', lambda **kw: creates.append(kw))
    plan['actions'].append(plan['actions'][0] | {'key': 'intro', 'task_type': 'email',
        'draft': {'recipient': 'ana@example.test', 'subject': 'Intro', 'body_html': '<p>Olá</p>'}})
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id)
        session.add(Activity(workspace_id=workspace, lead_id=sibling.id if lead_on_activity else None,
            account_id=sibling.account_id,
            activity_type=kind, title='Later company context', summary=summary,
            occurred_at=datetime.now(UTC), source_system='manual', actor_type='user'))
    context = client.get(f'/api/v1/agent/work/{work_id}', headers=headers()).json()['context']['note_source']
    assert summary in [x['summary'] for x in context['newer_context']['items']]
    response = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert response.status_code == 409, response.text
    assert creates == []
    assert_no_effects(engine, workspace, work_id, source_id)


def test_later_occurrence_is_not_hidden_by_earlier_import_creation(work_api):
    client, engine, workspace, lead_id = work_api
    work_id, source_id, plan = prepare(work_api)
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id)
        session.add(Activity(workspace_id=workspace, lead_id=sibling.id, account_id=sibling.account_id,
            activity_type='note', title='Later event', summary='Não contactar.',
            created_at=datetime.now(UTC)-timedelta(days=1), occurred_at=datetime.now(UTC),
            source_system='manual', actor_type='user'))
    response = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert response.status_code == 409, response.text
    assert_no_effects(engine, workspace, work_id, source_id)


def test_explicit_adoption_cannot_hide_other_company_manual_task(work_api):
    client, engine, workspace, lead_id = work_api
    work_id, source_id, plan = prepare(work_api)
    with Session(engine) as session, session.begin():
        sibling = sibling_lead(session, workspace, lead_id)
        tasks = [Task(workspace_id=workspace, lead_id=lid, task_type='call', title='Human callback',
            due_at=datetime.now(UTC)+timedelta(days=1), status='open', owner_user_id=uuid4())
            for lid in (lead_id, sibling.id)]
        session.add_all(tasks)
        session.flush()
        ids = [task.id for task in tasks]
        plan['actions'][0].update(existing_task_id=str(tasks[0].id), expected_task_version=tasks[0].version)
    response = client.post(f'/api/v1/agent/work/{work_id}/note-plan', json=plan, headers=headers())
    assert response.status_code == 409, response.text
    assert_no_effects(engine, workspace, work_id, source_id, ids)
