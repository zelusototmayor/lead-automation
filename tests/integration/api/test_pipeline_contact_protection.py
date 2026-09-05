from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.crm.persistence.models import Contact, Lead, SourceIdentity
from tests.integration.api.test_pipeline_api import pipeline_api
from tests.migration._postgres import require_disposable_postgres


@pytest.mark.parametrize("protection", ["terminal", "inactive", "suppressed_source"])
def test_protected_contacts_leave_operational_queues_but_keep_history(pipeline_api, protection):
    client, lead_id, _ = pipeline_api
    engine = create_engine(require_disposable_postgres())
    try:
        with Session(engine) as session, session.begin():
            lead = session.get(Lead, lead_id)
            if protection == "terminal":
                lead.stage = "lost"
            elif protection == "inactive":
                session.get(Contact, lead.contact_id).status = "inactive"
            else:
                source = SourceIdentity(workspace_id=lead.workspace_id, source_system="google_sheets",
                    source_scope="protected-fixture", entity_kind="lead", external_id=str(uuid4()),
                    metadata_json={"suppressed": True})
                session.add(source); session.flush(); lead.source_identity_id = source.id
        for queue in ("calls_today", "calls_future", "emails_today", "touched_today", "untouched"):
            response = client.get(f"/api/v1/pipeline/items?queue={queue}")
            assert response.status_code == 200, response.text
            assert str(lead_id) not in {row["lead_id"] for row in response.json()["items"]}
        assert str(lead_id) in {row["lead_id"] for row in client.get("/api/v1/pipeline/items?queue=all").json()["items"]}
        detail = client.get(f"/api/v1/leads/{lead_id}").json()
        assert detail["suppressed"] is True
        assert detail["company"] == "Acme Logistics"
        assert client.get(f"/api/v1/leads/{lead_id}/timeline").status_code == 200
    finally:
        engine.dispose()


def test_untouched_requires_new_stage_and_no_explicit_legacy_contact_receipt(pipeline_api):
    client, contacted_id, pre_account_contacted_id = pipeline_api
    engine = create_engine(require_disposable_postgres())
    try:
        initial = client.get("/api/v1/pipeline/items?queue=untouched").json()
        assert initial["total"] == 1
        new_id = initial["items"][0]["lead_id"]
        assert new_id not in {str(contacted_id), str(pre_account_contacted_id)}
        with Session(engine) as session, session.begin():
            from uuid import UUID
            lead = session.get(Lead, UUID(new_id))
            source = SourceIdentity(workspace_id=lead.workspace_id, source_system="google_sheets",
                source_scope="contact-proof-fixture", entity_kind="lead", external_id=str(uuid4()),
                metadata_json={"legacy_row": {"Stage": "New", "Initial Email Sent": "2024/02/03"}})
            session.add(source); session.flush(); lead.source_identity_id = source.id
        assert client.get("/api/v1/pipeline/items?queue=untouched").json()["total"] == 0
        assert client.get("/api/v1/pipeline/summary").json()["queues"]["untouched"] == 0
        assert client.get("/api/v1/pipeline/items?queue=all").json()["total"] == 3
    finally:
        engine.dispose()
