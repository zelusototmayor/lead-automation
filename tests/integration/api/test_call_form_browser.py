"""Real browser -> real application -> disposable PostgreSQL, no network.

Requires optional playwright + installed Chromium. All browser requests are
intercepted and passed to the in-process TestClient; non-local hosts are denied.
"""
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.crm.persistence.models import Activity
from tests.integration.api.test_lead_operations_api import lead_operations_api


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('mode', ['explicit', 'connected', 'no_answer', 'note_only'])
def test_real_form_restores_dimensions_and_canonically_saves(lead_operations_api, width, mode):
    playwright = pytest.importorskip('playwright.sync_api')
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    payloads, errors, rejected = [], [], []

    def in_process(route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != 'localhost:8000':
            rejected.append(url.netloc)
            route.fulfill(status=204, body='')
            return
        if request.method == 'POST':
            assert url.path == f'/api/v1/commands/leads/{lead_id}/log-call'
            payloads.append(request.post_data_json)
        response = client.request(request.method, request.url,
            content=request.post_data_buffer, headers=request.headers)
        route.fulfill(status=response.status_code, body=response.content,
            headers={k: v for k, v in response.headers.items()
                     if k not in {'content-length', 'content-encoding', 'transfer-encoding'}})

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 960},
            timezone_id='Europe/Lisbon', service_workers='block')
        page.route('**/*', in_process)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://localhost:8000/leads?queue=all&lead={lead_id}')
        form = page.locator('[data-call-log-form]')
        playwright.expect(form).to_be_visible()
        advanced = form.locator('[data-call-advanced]')
        playwright.expect(advanced).not_to_have_attribute('open')
        outcome = 'no_answer' if mode == 'no_answer' else 'connected'
        form.locator('[name=summary]').fill('Browser fixture: conversei com a receção.')
        if mode != 'note_only':
            advanced.locator('summary').click()
            form.locator(f'[name=outcome_code][value={outcome}]').check()
        values = {'answer_kind': 'human_counterparty',
                            'contact_kind': 'first_contact',
                            'first_conversation': 'yes',
                            'interlocutor_role': 'reception'} if mode == 'explicit' else {}
        for name, value in values.items():
            form.locator(f'[name={name}]').select_option(value)
        page.reload()
        playwright.expect(form).to_be_visible()
        if mode != 'note_only':
            advanced.locator('summary').click()
            playwright.expect(form.locator('[name=contact_kind]')).to_have_value(values.get('contact_kind', 'unknown'))
            playwright.expect(form.locator('[name=first_conversation]')).to_have_value(values.get('first_conversation', 'unknown'))
            advanced.locator('summary').click()
        playwright.expect(advanced).not_to_have_attribute('open')
        with page.expect_response(lambda r: r.request.method == 'POST') as saved:
            form.locator('[data-call-save]').click()
        assert saved.value.status == 200
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload['call_details']['answer_kind'] == values.get('answer_kind', 'unknown')
        assert payload['call_details']['contact_kind'] == values.get('contact_kind', 'unknown')
        assert payload['call_details']['first_conversation'] is (True if mode == 'explicit' else None)
        assert payload['call_details']['useful'] is None
        assert payload['call_details']['decision_maker'] is None
        assert payload['call_details']['interlocutor_role'] == values.get('interlocutor_role', 'unknown')
        assert payload['occurred_at'].endswith('Z')
        assert not errors
        with Session(engine) as session:
            calls = session.scalars(select(Activity).where(
                Activity.workspace_id == workspace_id,
                Activity.activity_type == 'call')).all()
            assert len(calls) == 1
            assert calls[0].call_details == payload['call_details']
            assert calls[0].summary == payload['summary']
        metrics = client.get('/api/v1/pipeline/call-metrics').json()
        assert metrics['counts']['attempts'] == 1
        assert metrics['counts']['answered'] == int(outcome == 'connected' and mode != 'note_only')
        if mode == 'note_only':
            assert payload['outcome_code'] is None
            assert metrics['coverage']['answer_unknown'] == 1
        assert metrics['counts']['useful'] == metrics['counts']['decision_maker'] == 0
        assert metrics['contact_counts']['first_contact'] == int(mode == 'explicit')
        assert metrics['deficit'] is None
        playwright.expect(page.locator('[data-call-metrics]')).to_have_count(0)
        page.locator('[data-open-analysis]').click()
        playwright.expect(page.locator('[data-analysis-total="call_initial"]')).to_have_text('1' if mode == 'explicit' else '—')
        browser.close()
