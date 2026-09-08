from datetime import datetime, UTC
from uuid import uuid4
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers
from tests.integration.api.test_call_cadence_api import details


def test_metrics_count_explicit_human_answers_without_claiming_legacy_first(lead_operations_api):
    client, engine, workspace_id, lead_id, actor_id = lead_operations_api
    command = uuid4()
    response = client.post(f'/api/v1/commands/leads/{lead_id}/log-call', headers=_headers(command),
        json={'command_id':str(command),'expected_version':1,'outcome_code':'not_interested',
              'call_details':details(useful=False)})
    assert response.status_code == 200, response.text
    day = datetime.now(UTC).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Lisbon')).date()
    response = client.get(f'/api/v1/pipeline/call-metrics?date={day}')
    assert response.status_code == 200
    data = response.json()
    assert data['counts']['answered'] == 1
    assert data['counts']['useful'] == 0
    assert data['counts']['first_answered_recorded'] == 1
    assert data['counts']['first_answered'] is None
    assert data['coverage']['legacy_history_unknown'] == 1
    assert data['deficit'] is None
    assert data['recorded_deficit'] == 9


def test_first_answered_can_be_explicitly_confirmed_by_human(lead_operations_api):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    command=uuid4()
    response=client.post(f'/api/v1/commands/leads/{lead_id}/log-call',headers=_headers(command),
        json={'command_id':str(command),'expected_version':1,'outcome_code':'connected',
              'call_details':details(useful=True, first_conversation=True)})
    assert response.status_code==200, response.text
    metric=client.get('/api/v1/pipeline/call-metrics').json()
    assert metric['counts']['first_answered_confirmed']==1
    assert metric['confirmed_deficit']==9
