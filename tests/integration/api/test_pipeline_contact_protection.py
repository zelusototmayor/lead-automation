from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.crm.persistence.models import Activity, Contact, Lead, SourceIdentity
from tests.integration.api.test_pipeline_api import pipeline_api
from tests.migration._postgres import require_disposable_postgres


def _only_untouched_lead_id(client) -> UUID:
    payload = client.get("/api/v1/pipeline/items?queue=untouched").json()
    assert payload["total"] == 1
    return UUID(payload["items"][0]["lead_id"])


def _insert_activity_for_lead(
    *,
    session: Session,
    lead_id: UUID,
    activity_type: str,
    occurred_at: datetime,
    title: str,
    **values,
) -> None:
    lead = session.get(Lead, lead_id)
    session.add(
        Activity(
            workspace_id=lead.workspace_id,
            account_id=lead.account_id,
            lead_id=lead.id,
            contact_id=lead.contact_id,
            activity_type=activity_type,
            occurred_at=occurred_at,
            title=title,
            **values,
        )
    )


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


@pytest.mark.parametrize(
    ("activity_type", "activity_values", "expected_touched_today"),
    [
        ("note", {}, 2),
        (
            "stage_change",
            {
                "semantic_fingerprint": "a" * 64,
                "from_stage": "new",
                "to_stage": "contacted",
            },
            2,
        ),
        ("task", {}, 1),
        ("system", {}, 1),
    ],
)
def test_internal_bookkeeping_activity_does_not_remove_new_lead_from_untouched(
    pipeline_api, activity_type, activity_values, expected_touched_today
):
    client, _, _ = pipeline_api
    lead_id = _only_untouched_lead_id(client)
    engine = create_engine(require_disposable_postgres())
    try:
        with Session(engine) as session, session.begin():
            _insert_activity_for_lead(
                session=session,
                lead_id=lead_id,
                activity_type=activity_type,
                occurred_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
                title=f"Internal {activity_type}",
                **activity_values,
            )

        untouched = client.get("/api/v1/pipeline/items?queue=untouched")
        touched_today = client.get("/api/v1/pipeline/items?queue=touched_today")

        assert untouched.status_code == touched_today.status_code == 200
        assert untouched.json()["total"] == 1
        assert {item["lead_id"] for item in untouched.json()["items"]} == {str(lead_id)}
        assert touched_today.json()["total"] == expected_touched_today
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("activity_type", "activity_values"),
    [
        ("call", {"outcome_code": "no_answer"}),
        ("email_sent", {}),
        ("email_received", {}),
        ("meeting", {}),
        ("proposal", {}),
    ],
)
def test_external_operational_activity_removes_new_lead_from_untouched(
    pipeline_api, activity_type, activity_values
):
    client, _, _ = pipeline_api
    lead_id = _only_untouched_lead_id(client)
    engine = create_engine(require_disposable_postgres())
    try:
        with Session(engine) as session, session.begin():
            _insert_activity_for_lead(
                session=session,
                lead_id=lead_id,
                activity_type=activity_type,
                occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
                title=f"External {activity_type}",
                **activity_values,
            )

        assert client.get("/api/v1/pipeline/items?queue=untouched").json()["total"] == 0
        assert client.get("/api/v1/pipeline/summary").json()["queues"]["untouched"] == 0
        assert client.get("/api/v1/pipeline/items?queue=all").json()["total"] == 3
    finally:
        engine.dispose()


def test_non_operational_external_activity_does_not_remove_new_lead_from_untouched(
    pipeline_api,
):
    client, _, _ = pipeline_api
    lead_id = _only_untouched_lead_id(client)
    engine = create_engine(require_disposable_postgres())
    try:
        with Session(engine) as session, session.begin():
            _insert_activity_for_lead(
                session=session,
                lead_id=lead_id,
                activity_type="call",
                occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
                title="Imported historical call context",
                actor_type="import",
                source_system="google_sheets",
            )

        untouched = client.get("/api/v1/pipeline/items?queue=untouched")

        assert untouched.status_code == 200
        assert untouched.json()["total"] == 1
        assert {item["lead_id"] for item in untouched.json()["items"]} == {str(lead_id)}
    finally:
        engine.dispose()
