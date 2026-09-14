"""R1: mounted plan saves use current exact obligations, never snapshot versions."""
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.crm.persistence.models import Activity, CallDayPlan, Lead, Task
from tests.integration.api.test_lead_operations_api import lead_operations_api


@pytest.mark.parametrize('current_status', ['open', 'completed', 'cancelled', 'missing', 'lost_response'])
def test_plan_callback_completes_exact_current_obligation(lead_operations_api, current_status):
    from playwright.sync_api import sync_playwright, expect

    client, engine, workspace, lead_id, actor_id = lead_operations_api
    selected, other = uuid4(), uuid4()
    day = datetime.now(ZoneInfo('Europe/Lisbon')).date()
    due = datetime.now(UTC) - timedelta(hours=1)
    plan = dict(schema_version=1, version=1, total=1, work_date=str(day), date=str(day),
                capacity_minutes=120, items=[dict(lead_id=str(lead_id), company='Original Company',
                cohort='calls_actionable', task=dict(id=str(selected), type='call', status='open',
                version=1, title='Selected callback', due_at=due.isoformat()))])
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        for task_id in (selected, other):
            session.add(Task(id=task_id, workspace_id=workspace, account_id=lead.account_id,
                             lead_id=lead_id, task_type='call', title='Callback', due_at=due,
                             status='open', owner_user_id=actor_id))
        session.flush()
        session.get(Task, selected).version = 3
        session.add(CallDayPlan(workspace_id=workspace, work_date=day, version=1, payload=plan,
                               command_id=uuid4(), actor_id=actor_id, request_hash='fixture'))
    payloads, errors = [], []

    def in_process(route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != 'localhost:8000':
            route.fulfill(status=204, body='')
            return
        if request.method != 'GET':
            assert request.method == 'POST'
            assert url.path == f'/api/v1/commands/leads/{lead_id}/log-call'
            payloads.append(request.post_data_json)
        response = client.request(request.method, request.url,
                                  content=request.post_data_buffer, headers=request.headers)
        if request.method == 'POST' and current_status == 'lost_response' and len(payloads) == 1:
            assert response.status_code == 200
            route.abort('failed')
            return
        route.fulfill(status=response.status_code, body=response.content,
                      headers={k: v for k, v in response.headers.items()
                               if k not in {'content-length', 'content-encoding', 'transfer-encoding'}})

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(timezone_id='Europe/Lisbon', service_workers='block')
            page.route('**/*', in_process)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://localhost:8000/leads')
            page.locator('[data-leads-list] .lead-open-button').click()
            form = page.locator('[data-call-log-form]')
            expect(form).to_be_visible()
            form.locator('[data-call-advanced] > summary').click()
            form.locator('[name=outcome_code][value=connected]').check()
            form.locator('[name=summary]').fill('Plan callback fixture')
            form.locator('[name=callback_enabled]').check()
            form.locator('[name=callback_agreed]').check()
            form.locator('[name=callback_due_at]').fill(
                (datetime.now(ZoneInfo('Europe/Lisbon')) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'))
            form.locator('[name=callback_title]').fill('New agreed callback')
            # Change the task after opening the detail: submit must read current state.
            with Session(engine) as session, session.begin():
                task = session.get(Task, selected)
                if current_status == 'missing':
                    session.delete(task)
                else:
                    task.status = 'open' if current_status == 'lost_response' else current_status
                    if current_status == 'completed':
                        completion = Activity(workspace_id=workspace, account_id=task.account_id,
                            lead_id=lead_id, activity_type='note', title='Completed elsewhere',
                            occurred_at=datetime.now(UTC), source_system='manual',
                            actor_type='human', actor_id=actor_id)
                        session.add(completion)
                        session.flush([completion])
                        task.completed_at = datetime.now(UTC)
                        task.completion_activity_id = completion.id
            with Session(engine) as session:
                task = session.get(Task, selected)
                before_version = task.version if task else None
            if current_status == 'lost_response':
                form.locator('[data-call-save]').click()
                expect(page.locator('[data-call-save-state]')).to_contain_text('Não foi possível confirmar')
                page.reload()
                expect(form).to_be_visible()
            with page.expect_response(lambda r: r.request.method == 'POST') as saved:
                form.locator('[data-call-save]').click()
            assert saved.value.status == 200
            assert len(payloads) == (2 if current_status == 'lost_response' else 1)
            if current_status in ('open', 'lost_response'):
                assert payloads[0].get('completed_task') == dict(id=str(selected), expected_version=before_version)
            else:
                assert 'completed_task' not in payloads[0]
            if current_status == 'lost_response':
                assert payloads[0] == payloads[1]
                assert saved.value.json()['replayed'] is True
            assert errors == []
            browser.close()
        with Session(engine) as session:
            task = session.get(Task, selected)
            if current_status == 'missing':
                assert task is None
            elif current_status in ('open', 'lost_response'):
                assert task.status == 'completed'
                assert before_version is not None
                assert task.version == before_version + 1
            else:
                assert task.status == current_status
                assert task.version == before_version
            assert session.get(Task, other).status == 'open'
            assert session.get(Task, other).version == 1
            new_tasks = session.scalars(select(Task).where(Task.workspace_id == workspace,
                Task.id.not_in([selected, other]))).all()
            assert len(new_tasks) == 1
            assert new_tasks[0].status == 'open'
            assert new_tasks[0].call_intent['agreed_with_client'] is True
            assert new_tasks[0].call_intent['calendar_policy'] == 'none'
            assert len(session.scalars(select(Activity).where(
                Activity.workspace_id == workspace, Activity.activity_type == 'call')).all()) == 1
            persisted = session.get(CallDayPlan, (workspace, day))
            assert persisted.payload == plan
            assert persisted.version == 1
    finally:
        with Session(engine) as session, session.begin():
            session.delete(session.get(CallDayPlan, (workspace, day)))
