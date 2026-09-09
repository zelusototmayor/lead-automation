from datetime import UTC, datetime
from unittest.mock import patch
import pytest
from uuid import uuid4
from sqlalchemy.orm import Session
from src.crm.persistence.models import Activity, Lead, SourceIdentity
from tests.integration.api.test_call_cadence_api import details

from tests.integration.api.test_lead_operations_api import lead_operations_api


def test_empty_analysis_is_four_partial_series_not_fabricated_history(lead_operations_api):
    client, *_ = lead_operations_api
    with patch('dashboard.app.routers.pipeline._utc_now', return_value=datetime(2026, 9, 9, 12, tzinfo=UTC)):
        response = client.get('/api/v1/pipeline/activity-analysis')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['timezone'] == 'Europe/Lisbon'
    assert data['days'] == 30
    assert set(data['series']) == {'email_initial', 'email_follow_up', 'call_initial', 'call_follow_up'}
    assert data['source_status'] == 'partial'
    for series in data['series'].values():
        assert series['total'] is None
        assert len(series['points']) == 30
        assert series['points'][-1]['date'] == '2026-09-09'
    assert client.get('/api/v1/pipeline/activity-analysis?days=8').status_code == 422


def test_calls_classify_attempts_before_event_and_bucket_lisbon(lead_operations_api):
    client, engine, workspace, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        for hour, kind in [(22, 'first_contact'), (23, 'first_contact')]:
            session.add(Activity(workspace_id=workspace, lead_id=lead_id, account_id=session.get(Lead, lead_id).account_id,
                activity_type='call', title='Fixture', outcome_code='no_answer',
                occurred_at=datetime(2026, 9, 8, hour, 30, tzinfo=UTC),
                call_details=details(answer_kind='no_answer', useful=None, contact_kind=kind)))
    with patch('dashboard.app.routers.pipeline._utc_now', return_value=datetime(2026, 9, 9, 12, tzinfo=UTC)):
        data = client.get('/api/v1/pipeline/activity-analysis?days=7').json()
    assert data['series']['call_initial']['total'] == 1
    assert data['series']['call_follow_up']['total'] == 1
    assert data['series']['call_initial']['points'][-2]['value'] == 1
    assert data['series']['call_follow_up']['points'][-1]['value'] == 1
    assert data['coverage']['call_unknown'] == 0


def test_emails_dedup_receipts_and_never_call_first_observed_initial(lead_operations_api):
    client, engine, workspace, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        source = SourceIdentity(workspace_id=workspace, source_system='gmail',
            source_scope='fixture-mailbox', entity_kind='message', external_id='fixture-sent')
        session.add(source)
        session.flush()
        for hour, identity in [(8, None), (9, source.id), (9, source.id)]:
            session.add(Activity(workspace_id=workspace, lead_id=lead_id, account_id=session.get(Lead, lead_id).account_id,
                activity_type='email_sent', title='Fixture', direction='outbound',
                source_system='gmail' if identity else None, source_identity_id=identity,
                occurred_at=datetime(2026, 9, 9, hour, tzinfo=UTC)))
    with patch('dashboard.app.routers.pipeline._utc_now', return_value=datetime(2026, 9, 9, 12, tzinfo=UTC)):
        data = client.get('/api/v1/pipeline/activity-analysis').json()
    assert data['series']['email_follow_up']['total'] == 1
    assert data['series']['email_initial']['total'] is None
    assert data['coverage']['email_without_message_identity'] == 1


@pytest.mark.parametrize('legacy,kind,expected', [
    ('2026-09-08', 'first_contact', 'call_follow_up'),
    ('2026-09-09', 'first_contact', None),
    ('invalid', 'first_contact', None),
    ('', 'unknown', None),
])
def test_legacy_uncertainty_and_unknown_are_not_zero(lead_operations_api, legacy, kind, expected):
    client, engine, workspace, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        source = SourceIdentity(workspace_id=workspace, source_system='google_sheets',
            source_scope='fixture-sheet', entity_kind='lead', external_id='fixture-row',
            metadata_json={'legacy_row': {'Initial Email Sent': legacy}})
        session.add(source); session.flush(); lead.source_identity_id = source.id
        session.add(Activity(workspace_id=workspace, lead_id=lead_id, account_id=lead.account_id,
            activity_type='call', title='Fixture', outcome_code='no_answer',
            occurred_at=datetime(2026, 9, 9, 10, tzinfo=UTC),
            call_details=details(answer_kind='no_answer', useful=None, contact_kind=kind)))
    with patch('dashboard.app.routers.pipeline._utc_now', return_value=datetime(2026, 9, 9, 12, tzinfo=UTC)):
        response = client.get('/api/v1/pipeline/activity-analysis?days=90')
    data = response.json()
    assert data['series']['call_initial']['total'] is None
    assert data['series']['call_follow_up']['total'] == (1 if expected else None)
    assert data['coverage']['call_unknown'] == (0 if expected else 1)
    assert 'fixture' not in response.text.lower()
    assert str(lead_id) not in response.text


def test_initial_email_unknown_and_import_inbound_excluded(lead_operations_api):
    client, engine, workspace, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        for index, (activity_type, actor, matched) in enumerate([
            ('email_sent', 'migration', True), ('email_received', None, True), ('email_sent', None, True)
        ]):
            source = SourceIdentity(workspace_id=workspace, source_system='gmail',
                source_scope='fixture-box', entity_kind='message', external_id=f'fixture-{index}')
            session.add(source); session.flush()
            session.add(Activity(workspace_id=workspace, lead_id=lead_id if matched else None,
                account_id=lead.account_id if matched else None, activity_type=activity_type,
                actor_type=actor, title='Fixture', source_system='gmail', source_identity_id=source.id,
                direction='inbound' if activity_type == 'email_received' else 'outbound',
                occurred_at=datetime(2026, 9, 9, 11 if activity_type == 'email_received' else 8 + index, tzinfo=UTC)))
    with patch('dashboard.app.routers.pipeline._utc_now', return_value=datetime(2026, 9, 9, 12, tzinfo=UTC)):
        data = client.get('/api/v1/pipeline/activity-analysis').json()
    assert data['series']['email_initial']['total'] is None
    assert data['series']['email_follow_up']['total'] is None
    assert data['coverage']['email_unknown'] == 1
