"""Mounted app QA: canonical plan, detail selection, analysis and no writes."""
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session
from src.crm.persistence.models import CallDayPlan
from tests.integration.api.test_lead_operations_api import lead_operations_api


@pytest.mark.parametrize('width', [1440, 390])
def test_plan_left_and_analysis_separate(lead_operations_api, width):
    from playwright.sync_api import sync_playwright, expect
    client, engine, workspace, lead_id, _ = lead_operations_api
    day = datetime.now(ZoneInfo('Europe/Lisbon')).date()
    plan = dict(schema_version=1, version=1, total=1, work_date=str(day), date=str(day), capacity_minutes=120,
                items=[dict(lead_id=str(lead_id), company='Original Company', cohort='phone_new', task=None)])
    with Session(engine) as session, session.begin():
        session.add(CallDayPlan(workspace_id=workspace, work_date=day, version=1, payload=plan,
                                command_id=uuid4(), actor_id=uuid4(), request_hash='fixture'))
    requests, errors = [], []
    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != 'localhost:8000':
            route.fulfill(status=204, body='')
            return
        requests.append((request.method, url.path))
        assert request.method == 'GET'
        response = client.get(request.url)
        route.fulfill(status=response.status_code, body=response.content,
                      headers={k:v for k,v in response.headers.items() if k not in {'content-length','content-encoding'}})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width':width, 'height':960}, timezone_id='Europe/Lisbon')
        page.route('**/*', route_request)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://localhost:8000/leads')
        expect(page.locator('.queue-pane [data-call-day]')).to_be_visible()
        expect(page.locator('[data-leads-list] .lead-row')).to_have_count(1)
        expect(page.locator('[data-call-metrics]')).to_have_count(0)
        assert not any(path.endswith('activity-analysis') for _,path in requests)
        expect(page.locator('[data-activity-analysis]')).to_be_hidden()
        page.locator('[data-leads-list] .lead-open-button').click()
        expect(page.locator('[data-detail-company]')).to_have_text('Original Company')
        if width == 390:
            page.locator('[data-back-to-queue]').click()
        evidence = Path('.task-evidence'); evidence.mkdir(exist_ok=True)
        page.screenshot(path=str(evidence/f'after-calls-{width}.png'), full_page=True)
        page.locator('[data-open-analysis]').click()
        expect(page.locator('[data-activity-analysis]')).to_be_visible()
        expect(page.locator('.pipeline-layout')).to_be_hidden()
        expect(page.locator('[data-analysis-total]')).to_have_count(4)
        expect(page.locator('[data-analysis-period]')).to_have_value('30')
        for days in ('7', '90', '30'):
            page.locator('[data-analysis-period]').select_option(days)
            expect(page.locator('[data-analysis-range]')).to_contain_text(f'{days} dias')
        expect(page.locator('[data-analysis-chart] svg')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(evidence/f'after-analysis-{width}.png'), full_page=True)
        page.locator('[data-close-analysis]').click()
        expect(page.locator('.pipeline-layout')).to_be_visible()
        assert errors == []
        browser.close()
    with Session(engine) as session, session.begin():
        session.delete(session.get(CallDayPlan, (workspace, day)))
