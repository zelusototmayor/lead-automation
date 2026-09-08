"""Regression coverage on a disposable DB, never CRM data repair."""
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from src.crm.persistence.models import Activity, Lead
from tests.integration.api.test_call_cadence_api import details
from tests.integration.api.test_lead_operations_api import lead_operations_api


@pytest.mark.parametrize('outcome', ['no_answer', 'voicemail', 'wrong_number'])
def test_persisted_conflict_cannot_certify_human_dimensions(lead_operations_api, outcome):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    # Historic/imported v1 JSON can predate stricter canonical writer checks.
    # The test DB accepts this shape; production code must never repair it.
    with Session(engine) as session, session.begin():
        session.add(Activity(workspace_id=workspace_id, lead_id=lead_id,
            account_id=session.get(Lead, lead_id).account_id,
            activity_type='call', title='Contradictory retained evidence',
            occurred_at=datetime.now(UTC), outcome_code=outcome,
            call_details=details(useful=True, first_conversation=True,
                decision_maker=True, interlocutor_role='decision_maker')))
    metrics = client.get('/api/v1/pipeline/call-metrics').json()
    assert metrics['counts']['attempts'] == 1
    assert metrics['counts']['answered'] == 0
    assert metrics['counts']['useful'] == 0
    assert metrics['counts']['decision_maker'] == 0
    assert metrics['counts']['first_answered_confirmed'] == 0
    assert metrics['coverage']['answer_unknown'] == 1
    assert metrics['deficit'] is None


@pytest.mark.parametrize('note,matched', [
    ('Falei com a receção; pediu para voltar a ligar.', True),
    ('Não falei com a receção; não houve conversa.', False),
    ('Talvez a receção possa falar amanhã.', False),
    ('Falei com a receção? Não tenho confirmação.', False),
])
def test_reviewed_note_requires_exact_positive_evidence(lead_operations_api, monkeypatch, note, matched):
    from hashlib import sha256
    from uuid import uuid4
    from src.crm.services import call_metrics as module
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    event_id = uuid4()
    reviewed_text = 'Falei com a receção; pediu para voltar a ligar.'
    monkeypatch.setattr(module, 'REVIEWED_LEGACY_ANSWERS', {
        str(event_id): ('follow_up', sha256(reviewed_text.encode()).hexdigest())})
    with Session(engine) as session, session.begin():
        session.add(Activity(id=event_id, workspace_id=workspace_id, lead_id=lead_id,
            account_id=session.get(Lead, lead_id).account_id,
            activity_type='call', title='Retained note', occurred_at=datetime.now(UTC),
            outcome_code='follow_up', summary=note))
    metrics = client.get('/api/v1/pipeline/call-metrics').json()
    assert metrics['counts']['answered'] == int(matched)
    assert metrics['coverage']['answer_unknown'] == int(not matched)
    assert metrics['counts']['useful'] == metrics['counts']['decision_maker'] == 0
    assert metrics['counts']['first_answered_confirmed'] == 0


@pytest.mark.parametrize('kind', ['no_answer', 'voicemail', 'ivr', 'wrong_number'])
def test_connected_conflicting_with_explicit_nonhuman_stays_unknown(lead_operations_api, kind):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        session.add(Activity(workspace_id=workspace_id, lead_id=lead_id,
            account_id=session.get(Lead, lead_id).account_id,
            activity_type='call', title='Specific retained evidence',
            occurred_at=datetime.now(UTC), outcome_code='connected',
            call_details=details(answer_kind=kind)))
    metrics = client.get('/api/v1/pipeline/call-metrics').json()
    assert metrics['counts']['answered'] == 0
    assert metrics['coverage']['answer_unknown'] == 1
    assert any('conflito' in blocker for blocker in metrics['blockers'])


@pytest.mark.parametrize('day,start,end', [
    ('2026-03-29', '2026-03-29T00:00:00+00:00', '2026-03-29T23:00:00+00:00'),
    ('2026-10-25', '2026-10-24T23:00:00+00:00', '2026-10-26T00:00:00+00:00'),
])
def test_lisbon_dst_day_is_half_open_without_event_limit(lead_operations_api, day, start, end):
    from datetime import timedelta
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
    # More than an API page, and both DST transitions (23h/25h days).
    inside = [start + timedelta(minutes=i) for i in range(107)] + [end - timedelta(microseconds=1)]
    with Session(engine) as session, session.begin():
        account_id = session.get(Lead, lead_id).account_id
        for when in [start - timedelta(microseconds=1), *inside, end]:
            session.add(Activity(workspace_id=workspace_id, lead_id=lead_id, account_id=account_id,
                activity_type='call', title='Boundary fixture',
                occurred_at=when, outcome_code='connected'))
    metrics = client.get(f'/api/v1/pipeline/call-metrics?date={day}').json()
    assert metrics['counts']['attempts'] == metrics['counts']['answered'] == len(inside)
    assert metrics['coverage']['answer_unknown'] == 0
    assert metrics['counts']['first_answered_confirmed'] == 0
    assert metrics['contact_counts']['follow_up'] == len(inside)
    assert metrics['timezone'] == 'Europe/Lisbon'
