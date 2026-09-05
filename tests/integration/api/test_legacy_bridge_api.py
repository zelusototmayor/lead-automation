from datetime import datetime, timezone, timedelta
from uuid import uuid4
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.db import get_database_settings
from dashboard.app.routers.legacy_bridge import router
from src.crm.persistence.models import (
    Workspace,
    Account,
    Contact,
    Lead,
    Task,
    Activity,
)
from tests.migration._postgres import require_disposable_postgres, cleanup_workspace


@pytest.fixture
def bridge(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    wid, owner, lid, callid, emailid = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    with Session(engine) as s, s.begin():
        s.add(Workspace(id=wid, slug="bridge-" + str(wid), name="Bridge test"))
        s.flush()
        account = Account(
            workspace_id=wid, display_name="Bridge test", normalized_name="bridge test"
        )
        s.add(account)
        s.flush()
        contact = Contact(
            workspace_id=wid,
            account_id=account.id,
            primary_email="contact@example.test",
        )
        s.add(contact)
        s.flush()
        s.add(
            Lead(id=lid, workspace_id=wid, account_id=account.id, contact_id=contact.id)
        )
        s.flush()
        s.add(
            Task(
                id=callid,
                workspace_id=wid,
                account_id=account.id,
                lead_id=lid,
                task_type="call",
                title="Independent callback",
                due_at=now + timedelta(days=3),
                owner_user_id=owner,
            )
        )
        s.add(
            Task(
                id=emailid,
                workspace_id=wid,
                account_id=account.id,
                lead_id=lid,
                task_type="email",
                title="Initial email",
                due_at=now,
                owner_user_id=owner,
                source_rule="release:legacy_initial_email",
            )
        )
    for key, value in {
        "CRM_DB_ENABLED": "true",
        "CRM_COMMAND_WRITER": "postgres",
        "CRM_AUTOMATION_BEARER_TOKEN": "z" * 40,
        "CRM_AUTOMATION_WORKSPACE_ID": str(wid),
        "CRM_AUTOMATION_SCOPES": "legacy:read,legacy:write",
        "CRM_AUTOMATION_SOURCE_SCOPES": "mailbox:sender@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    get_feature_flags.cache_clear()
    get_database_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, engine, wid, lid, callid, emailid
    cleanup_workspace(engine, wid)
    engine.dispose()
    get_feature_flags.cache_clear()
    get_database_settings.cache_clear()


def headers():
    return {
        "Authorization": "Bearer " + "z" * 40,
        "X-Agent-Timestamp": datetime.now(timezone.utc).isoformat(),
    }


def test_confirmed_receipt_is_idempotent_and_preserves_callback(bridge):
    c, e, w, l, call, email = bridge
    payload = {
        "lead_id": str(l),
        "task_id": str(email),
        "gmail_message_id": "abc123def456",
        "gmail_thread_id": "thread123456",
        "mailbox": "sender@example.test",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "summary": "Confirmed provider receipt",
    }
    first = c.post("/api/v1/agent/legacy/sent-receipt", json=payload, headers=headers())
    assert first.status_code == 200, first.text
    replay = c.post(
        "/api/v1/agent/legacy/sent-receipt", json=payload, headers=headers()
    )
    assert replay.json() == {"success": True, "replayed": True}
    with Session(e) as s:
        assert s.get(Task, email).status == "completed"
        assert s.get(Task, call).status == "open"
        assert (
            s.scalar(
                select(func.count())
                .select_from(Activity)
                .where(
                    Activity.workspace_id == w, Activity.activity_type == "email_sent"
                )
            )
            == 1
        )
    conflict = c.post(
        "/api/v1/agent/legacy/sent-receipt",
        json=payload | {"summary": "Changed payload"},
        headers=headers(),
    )
    assert conflict.status_code == 409


def test_no_access_without_scope_timestamp_or_token(bridge):
    c, *_ = bridge
    assert c.get("/api/v1/agent/legacy/leads").status_code == 401
    assert (
        c.get(
            "/api/v1/agent/legacy/leads",
            headers=headers() | {"Origin": "https://evil.test"},
        ).status_code
        == 401
    )
    assert (
        c.get(
            "/api/v1/agent/legacy/leads",
            headers=headers() | {"X-Agent-Timestamp": "2020-01-01T00:00:00Z"},
        ).status_code
        == 401
    )


def test_terminal_or_inactive_contacts_never_enter_sender_queue(bridge):
    c, e, w, l, call, email = bridge
    assert (
        len(
            c.get(
                "/api/v1/agent/legacy/outreach-followups?view=due", headers=headers()
            ).json()["tasks"]
        )
        == 1
    )
    with Session(e) as s, s.begin():
        lead = s.get(Lead, l)
        lead.stage = "not_a_fit"
    assert (
        c.get(
            "/api/v1/agent/legacy/outreach-followups?view=due", headers=headers()
        ).json()["tasks"]
        == []
    )
