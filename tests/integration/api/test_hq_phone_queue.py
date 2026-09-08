from tests.integration.api.test_lead_operations_api import lead_operations_api
from sqlalchemy.orm import Session
from src.crm.persistence.models import Lead, Contact


def test_unknown_history_callable_queue_is_not_certified_new(lead_operations_api):
    client, engine, workspace_id, lead_id, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        lead=session.get(Lead,lead_id)
        session.get(Contact,lead.contact_id).phone='+351210000001'
    summary=client.get('/api/v1/pipeline/summary').json()
    assert summary['queues'].get('phone_unknown') == 1
    assert summary['queues'].get('phone_new') == 0
    page=client.get('/api/v1/pipeline/items?queue=phone_unknown')
    assert page.status_code==200, page.text
    assert page.json()['total']==1
    assert page.json()['items'][0]['lead_id']==str(lead_id)
