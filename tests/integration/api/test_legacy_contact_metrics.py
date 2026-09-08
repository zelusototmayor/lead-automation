"""Every imported contact date must constrain explicit first-contact claims."""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from src.crm.persistence.models import Activity, Lead, SourceIdentity
from tests.integration.api.test_call_cadence_api import details
from tests.integration.api.test_lead_operations_api import lead_operations_api
from tests.unit.test_legacy_contact_dates import CONTACT_DATE_HEADERS


def seed_call(engine, workspace_id, lead_id, raw, call_details):
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        source = SourceIdentity(
            workspace_id=workspace_id, source_system='google_sheets',
            source_scope='legacy-contact-regression', entity_kind='lead',
            external_id=str(uuid4()), metadata_json={'legacy_row': raw},
        )
        session.add(source)
        session.flush()
        lead.source_identity_id = source.id
        session.add(Activity(
            workspace_id=workspace_id, lead_id=lead_id, account_id=lead.account_id,
            activity_type='call', title='Private contact regression',
            occurred_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
            outcome_code='no_answer', call_details=call_details,
        ))


@pytest.mark.parametrize('header', CONTACT_DATE_HEADERS)
@pytest.mark.parametrize('value, expected', [
    ('2026-09-07', 'follow_up'),
    ('2026-09-08', 'unknown'),
    ('not a date', 'unknown'),
])
def test_legacy_evidence_overrides_explicit_first_contact(
    lead_operations_api, header, value, expected
):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    seed_call(engine, workspace_id, lead_id, {header: value},
              details(answer_kind='no_answer', contact_kind='first_contact'))
    response = client.get('/api/v1/pipeline/call-metrics?date=2026-09-08')
    assert response.status_code == 200, response.text
    metric = response.json()
    assert metric['contact_counts'] == {
        'first_contact': 0, 'follow_up': int(expected == 'follow_up'),
        'unknown': int(expected == 'unknown'), 'new_companies': 0,
    }
    assert metric['counts']['answered'] == 0
    assert metric['deficit'] is None


@pytest.mark.parametrize('raw', [
    {}, None, [],
    {'Stage': 'New'},
    {header: '' for header in CONTACT_DATE_HEADERS},
    {header: '2026-09-09' for header in CONTACT_DATE_HEADERS},
    {'Due': '2026-09-07', 'Proposal Next Action Due': '2026-09-07',
     'Dashboard Touched': '2026-09-07'},
])
@pytest.mark.parametrize('call_details', [
    None, details(answer_kind='no_answer'),
    details(answer_kind='no_answer', contact_kind='unknown'),
])
def test_missing_history_never_infers_first_contact(lead_operations_api, raw, call_details):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    seed_call(engine, workspace_id, lead_id, raw, call_details)
    metric = client.get('/api/v1/pipeline/call-metrics?date=2026-09-08').json()
    assert metric['contact_counts'] == {
        'first_contact': 0, 'follow_up': 0, 'unknown': 1, 'new_companies': 0,
    }
