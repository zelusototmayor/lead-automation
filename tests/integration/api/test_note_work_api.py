from datetime import UTC, datetime, timedelta
from uuid import uuid4
from sqlalchemy.orm import Session
from src.crm.persistence.models import Activity, Lead
import pytest
from src.crm.services.note_source_service import note_context, source_digest
from src.crm.services.note_source_service import enqueue_note_source
from tests.integration.api.test_agent_work_api import work_api, headers, claim


def seed_source(engine, workspace, lead):
    with Session(engine) as session, session.begin():
        source = Activity(id=uuid4(), workspace_id=workspace, lead_id=lead, account_id=session.get(Lead, lead).account_id,
            activity_type="call", occurred_at=datetime.now(UTC), title="Call logged",
            summary="Receção encaminhou; confirmar endereço com ç antes da intro.",
            source_system="manual", actor_type="human")
        session.add(source)
        session.flush()
        work_id = enqueue_note_source(session, source)
        return source.id, work_id


def test_claim_contains_exact_source_and_pre_event_history(work_api):
    client, engine, workspace, lead = work_api
    source_id, work_id = seed_source(engine, workspace, lead)
    with Session(engine) as session, session.begin():
        account_id = session.get(Lead, lead).account_id
        session.add(Activity(workspace_id=workspace, lead_id=lead, account_id=account_id, activity_type="email_sent",
            occurred_at=datetime.now(UTC)-timedelta(days=1), title="Prior email", summary="Earlier contact"))
        session.add(Activity(workspace_id=workspace, lead_id=lead, account_id=account_id, activity_type="email_sent",
            occurred_at=datetime.now(UTC)+timedelta(days=1), title="Future email"))
    item = claim(client).json()["items"][0]
    assert item["id"] == str(work_id)
    context = item["context"]["note_source"]
    assert context["activity_id"] == str(source_id)
    assert context["summary"] == "Receção encaminhou; confirmar endereço com ç antes da intro."
    assert context["source_digest"] == item["payload"]["source_digest"]
    assert context["history"]["prior_contact_known"] is True
    assert [x["title"] for x in context["history"]["items"]] == ["Prior email"]
    assert context["history"]["all_channel_history_complete"] is False


@pytest.mark.parametrize("case", ["foreign_workspace", "foreign_lead", "missing_source", "changed_digest", "superseded"])
def test_context_fails_closed_for_stale_or_unscoped_source(work_api, case):
    _, engine, workspace, lead = work_api
    source_id, _ = seed_source(engine, workspace, lead)
    with Session(engine) as session, session.begin():
        source = session.get(Activity, source_id)
        payload = {"activity_id": str(source_id), "source_digest": source_digest(source)}
        if case == "foreign_workspace":
            workspace = uuid4()
        elif case == "foreign_lead":
            lead = uuid4()
        elif case == "missing_source":
            payload = {}
        elif case == "changed_digest":
            payload["source_digest"] = "0" * 64
        elif case == "superseded":
            session.add(Activity(workspace_id=workspace, lead_id=lead, account_id=source.account_id,
                activity_type="note", occurred_at=datetime.now(UTC), title="Human correction",
                summary="Correção: não preparar intro.", supersedes_activity_id=source.id))
            session.flush()
        value = note_context(session, workspace, lead, payload)
        expected = "superseded" if case == "superseded" else "source_changed" if case == "changed_digest" else "source_unavailable"
        assert value["status"] == expected
        if expected == "source_unavailable":
            assert "summary" not in value
