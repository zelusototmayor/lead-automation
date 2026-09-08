from uuid import uuid4
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers
from tests.integration.api.test_call_cadence_api import details

def test_explicit_prior_conversation_is_repeat_not_first_recorded(lead_operations_api):
    client,_,_,lead_id,_=lead_operations_api
    command=uuid4()
    response=client.post(f'/api/v1/commands/leads/{lead_id}/log-call',headers=_headers(command),
        json=dict(command_id=str(command),expected_version=1,outcome_code='connected',
        call_details=details(first_conversation=False)))
    assert response.status_code==200
    counts=client.get('/api/v1/pipeline/call-metrics').json()['counts']
    assert counts['repeats']==1
    assert counts['first_answered_recorded']==0
