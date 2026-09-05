from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.routers.agent_work import router, get_work_session
from src.crm.persistence.models import (
    Workspace,
    AgentWork,
    Lead,
    Account,
    Contact,
    Task,
    Proposal,
    ProposalVersion,
)
from src.crm.services.agent_work_service import enqueue_work, claim_work
from tests.migration._postgres import require_disposable_postgres, cleanup_workspace


@pytest.fixture
def work_api(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=workspace, slug=f"work-{workspace}", name="Work test"))
        session.flush()
        account = Account(
            workspace_id=workspace,
            display_name="Real Contact",
            normalized_name="real contact",
        )
        session.add(account)
        session.flush()
        contact = Contact(
            workspace_id=workspace,
            account_id=account.id,
            full_name="Ana",
            primary_email="ana@example.test",
        )
        session.add(contact)
        session.flush()
        lead = Lead(
            workspace_id=workspace,
            account_id=account.id,
            contact_id=contact.id,
            company_name="Real Contact",
            contact_email="ana@example.test",
        )
        session.add(lead)
        session.flush()
        lead_id = lead.id
    for key, value in {
        "CRM_DB_ENABLED": "true",
        "CRM_COMMAND_WRITER": "postgres",
        "CRM_AUTOMATION_BEARER_TOKEN": "a" * 40,
        "CRM_AUTOMATION_WORKSPACE_ID": str(workspace),
        "CRM_AUTOMATION_SCOPES": "work:read,work:write,providers:sync",
        "CRM_AUTOMATION_SOURCE_SCOPES": "mailbox:me@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    get_feature_flags.cache_clear()
    app = FastAPI()
    app.include_router(router)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_work_session] = session_override
    with TestClient(app) as client:
        yield client, engine, workspace, lead_id
    cleanup_workspace(engine, workspace)
    engine.dispose()
    get_feature_flags.cache_clear()


def headers():
    return {
        "Authorization": "Bearer " + "a" * 40,
        "X-Agent-Timestamp": datetime.now(UTC).isoformat(),
    }


def create_work(engine, workspace, lead_id):
    with Session(engine) as session, session.begin():
        return enqueue_work(
            session,
            workspace_id=workspace,
            source_key=str(uuid4()),
            kind="call_followup",
            lead_id=lead_id,
        )


def claim(client):
    return client.post(
        "/api/v1/agent/work/claim",
        json={"worker_id": "fixture", "limit": 20, "lease_seconds": 60},
        headers=headers(),
    )


def test_auth_workspace_boundaries_and_cookie_rejection(work_api):
    client, engine, workspace, lead = work_api
    create_work(engine, workspace, lead)
    assert client.get("/api/v1/agent/work").status_code == 401
    assert (
        client.get(
            "/api/v1/agent/work", headers=headers() | {"Origin": "https://example.test"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/agent/work",
            headers=headers() | {"X-Agent-Timestamp": "2000-01-01T00:00:00Z"},
        ).status_code
        == 401
    )
    assert len(client.get("/api/v1/agent/work", headers=headers()).json()["items"]) == 1
    forbidden = client.post(
        "/api/v1/agent/providers/observations",
        json={"observations": [{"source_scope": "someone-else"}]},
        headers=headers(),
    )
    assert forbidden.status_code == 403


def test_claim_finish_is_fenced_and_idempotent(work_api):
    client, engine, workspace, lead = work_api
    create_work(engine, workspace, lead)
    item = claim(client).json()["items"][0]
    assert claim(client).json()["items"] == []
    body = {
        "lease_token": item["lease_token"],
        "status": "waiting",
        "result": {"summary": "Precisa de decisão", "evidence": []},
    }
    uri = f"/api/v1/agent/work/{item['id']}/finish"
    assert (
        client.post(
            uri, json=body | {"lease_token": str(uuid4())}, headers=headers()
        ).status_code
        == 409
    )
    first = client.post(uri, json=body, headers=headers())
    assert first.status_code == 200
    assert client.post(uri, json=body, headers=headers()).json()["replayed"] is True
    assert (
        client.post(
            uri,
            json=body | {"result": {"summary": "Outra decisão", "evidence": []}},
            headers=headers(),
        ).status_code
        == 409
    )


def test_crash_recovery_bounded_three_attempts(work_api):
    client, engine, workspace, lead = work_api
    work_id = create_work(engine, workspace, lead)
    previous = None
    for attempt in range(1, 4):
        item = claim(client).json()["items"][0]
        assert item["attempt"] == attempt
        if previous:
            assert (
                client.post(
                    f"/api/v1/agent/work/{work_id}/finish",
                    json={"lease_token": previous, "result": {"summary": "stale"}},
                    headers=headers(),
                ).status_code
                == 409
            )
        previous = item["lease_token"]
        with Session(engine) as session, session.begin():
            row = session.get(AgentWork, work_id)
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert claim(client).json()["items"] == []
    with Session(engine) as session:
        assert session.get(AgentWork, work_id).status == "failed"


def test_concurrent_claim_has_one_owner(work_api):
    _, engine, workspace, lead = work_api
    create_work(engine, workspace, lead)

    def worker(n):
        with Session(engine) as session, session.begin():
            return claim_work(session, workspace, worker_id=str(n), limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [1, 2]))
    assert sum(len(r) for r in results) == 1


def observation(**changes):
    return {
        "provider": "gmail",
        "source_scope": "mailbox:me@example.test",
        "id": "msg-1",
        "thread_id": "thread-1",
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "direction": "outbound",
        "subject": "Proposta para Acme",
        "excerpt": "Total: 1200 EUR",
        "contact_emails": ["ana@example.test"],
        "attachments": [
            {
                "name": "proposta.pdf",
                "content_hash": "a" * 64,
                "currency": "EUR",
                "one_off_amount": "1200",
                "mrr_amount": None,
                "arr_amount": None,
                "value_ambiguous": False,
            }
        ],
        **changes,
    }


def test_real_observation_creates_proposal_candidate_and_replays(work_api):
    client, engine, workspace, lead = work_api
    payload = {"observations": [observation()]}
    first = client.post(
        "/api/v1/agent/providers/observations", json=payload, headers=headers()
    )
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 1
    assert (
        client.post(
            "/api/v1/agent/providers/observations", json=payload, headers=headers()
        ).json()["duplicates"]
        == 1
    )
    with Session(engine) as session:
        proposals = list(
            session.scalars(select(Proposal).where(Proposal.workspace_id == workspace))
        )
        assert len(proposals) == 1
        assert proposals[0].sent_verification_state == "verified"
        assert proposals[0].value_state == "candidate"
        versions = list(
            session.scalars(
                select(ProposalVersion).where(
                    ProposalVersion.proposal_id == proposals[0].id
                )
            )
        )
        assert len(versions) == 1
        assert versions[0].one_off_amount == 1200


def test_reply_next_action_preserves_human_callback(work_api):
    client, engine, workspace, lead = work_api
    with Session(engine) as session, session.begin():
        l = session.get(Lead, lead)
        callback = Task(
            workspace_id=workspace,
            lead_id=lead,
            account_id=l.account_id,
            task_type="call",
            title="Callback humano",
            due_at=datetime.now(UTC) + timedelta(days=1),
            owner_user_id=uuid4(),
            source_rule="manual_next_action",
        )
        session.add(callback)
        session.flush()
        callback_id = callback.id
    obs = observation(
        direction="inbound", attachments=[], excerpt="Voltar a contactar em outubro"
    )
    assert (
        client.post(
            "/api/v1/agent/providers/observations",
            json={"observations": [obs]},
            headers=headers(),
        ).status_code
        == 200
    )
    item = claim(client).json()["items"][0]
    result = client.post(
        f"/api/v1/agent/work/{item['id']}/next-action",
        json={
            "lease_token": item["lease_token"],
            "expected_lead_version": item["context"]["lead_version"],
            "title": "Retomar em outubro",
            "due_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "supersede_agent_followups": True,
        },
        headers=headers(),
    )
    assert result.status_code == 200, result.text
    with Session(engine) as session:
        assert session.get(Task, callback_id).status == "open"
        assert (
            len(
                list(
                    session.scalars(
                        select(Task).where(
                            Task.workspace_id == workspace, Task.status == "open"
                        )
                    )
                )
            )
            == 2
        )


def test_unrecognized_correspondent_stays_review_no_created_lead(work_api):
    client, engine, workspace, lead = work_api
    response = client.post(
        "/api/v1/agent/providers/observations",
        json={"observations": [observation(contact_emails=["unknown@example.test"])]},
        headers=headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["kind"] == "identity_review"
    with Session(engine) as session:
        assert (
            len(
                list(
                    session.scalars(select(Lead).where(Lead.workspace_id == workspace))
                )
            )
            == 1
        )
        assert not list(
            session.scalars(select(Proposal).where(Proposal.workspace_id == workspace))
        )


def test_ordinary_unmatched_email_does_not_wake_agent(work_api):
    client, engine, workspace, lead = work_api
    payload = {
        "observations": [
            observation(
                contact_emails=["newsletter@example.test"],
                attachments=[],
                direction="inbound",
            )
        ]
    }
    response = client.post(
        "/api/v1/agent/providers/observations", json=payload, headers=headers()
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["kind"] == "unmatched_observation"
    assert claim(client).json()["items"] == []
    assert client.get("/api/v1/agent/work", headers=headers()).json()["items"] == []


def test_followup_not_claimed_until_due_and_superseded_task_skipped(work_api):
    from src.crm.services.agent_work_service import enqueue_task_work

    client, engine, workspace, lead_id = work_api
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        task = Task(
            workspace_id=workspace,
            lead_id=lead_id,
            account_id=lead.account_id,
            task_type="email",
            title="Email amanhã",
            due_at=datetime.now(UTC) + timedelta(days=1),
            owner_user_id=uuid4(),
        )
        session.add(task)
        session.flush()
        job_id = enqueue_task_work(session, task)
        task_id = task.id
    assert claim(client).json()["items"] == []
    with Session(engine) as session, session.begin():
        row = session.get(AgentWork, job_id)
        row.available_at = datetime.now(UTC) - timedelta(minutes=1)
        task = session.get(Task, task_id)
        task.status = "cancelled"
    assert claim(client).json()["items"] == []
    with Session(engine) as session:
        assert session.get(AgentWork, job_id).status == "completed"


def test_claim_context_uses_canonical_account_contact(work_api):
    client, engine, workspace, lead_id = work_api
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        lead.company_name = None
        lead.contact_email = None
    create_work(engine, workspace, lead_id)
    item = claim(client).json()["items"][0]
    assert item["context"]["company"] == "Real Contact"
    assert item["context"]["contact_email"] == "ana@example.test"


def test_suppressed_contact_cannot_receive_agent_followup(work_api):
    client, engine, workspace, lead = work_api
    with Session(engine) as session, session.begin():
        record = session.get(Lead, lead)
        contact = session.get(Contact, record.contact_id)
        contact.status = "inactive"
    create_work(engine, workspace, lead)
    item = claim(client).json()["items"][0]
    assert item["context"]["suppressed"] is True
    response = client.post(
        f"/api/v1/agent/work/{item['id']}/next-action",
        json={
            "lease_token": item["lease_token"],
            "expected_lead_version": item["context"]["lead_version"],
            "title": "Retomar",
            "due_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        },
        headers=headers(),
    )
    assert response.status_code == 409
    with Session(engine) as session:
        assert not list(
            session.scalars(select(Task).where(Task.workspace_id == workspace))
        )


def test_deterministic_callback_claim_is_separate_from_reasoning_budget(work_api):
    client, engine, workspace, lead = work_api
    with Session(engine) as session, session.begin():
        enqueue_work(
            session,
            workspace_id=workspace,
            source_key="reasoning",
            kind="reply_review",
            lead_id=lead,
        )
        enqueue_work(
            session,
            workspace_id=workspace,
            source_key="calendar",
            kind="calendar_callback",
            lead_id=lead,
        )
    response = client.post(
        "/api/v1/agent/work/claim",
        json={
            "worker_id": "calendar-no-model",
            "limit": 3,
            "kinds": ["calendar_callback"],
        },
        headers=headers(),
    )
    assert response.status_code == 200
    assert [i["kind"] for i in response.json()["items"]] == ["calendar_callback"]
    with Session(engine) as session:
        remaining = session.scalar(
            select(AgentWork).where(
                AgentWork.workspace_id == workspace, AgentWork.source_key == "reasoning"
            )
        )
        assert remaining.status == "queued" and remaining.attempts == 0


def waiting_review(work_api, *, kind="proposal_review", result=None):
    client, engine, workspace, lead = work_api
    with Session(engine) as session, session.begin():
        work_id = enqueue_work(
            session,
            workspace_id=workspace,
            source_key=str(uuid4()),
            kind=kind,
            lead_id=lead,
        )
    item = claim(client).json()["items"][0]
    result = result or {
        "summary": "Documento precisa de revisão",
        "evidence": [{"message_id": "mail-1"}],
    }
    response = client.post(
        f"/api/v1/agent/work/{work_id}/finish",
        headers=headers(),
        json={
            "lease_token": item["lease_token"],
            "status": "waiting",
            "result": result,
        },
    )
    assert response.status_code == 200
    return client.get(f"/api/v1/agent/work/{work_id}", headers=headers()).json()


def review_resolution(item):
    return {
        "resolution_id": str(uuid4()),
        "expected_result_hash": item["result_hash"],
        "result": {
            "summary": "Documento confirmado como apresentação, sem proposta financeira",
            "evidence": [
                {
                    "attachment_sha256": "a" * 64,
                    "classification": "brochure",
                    "verified": True,
                }
            ],
        },
    }


def test_review_resolution_auth_scope_and_foreign_workspace(work_api, monkeypatch):
    client, engine, workspace, lead = work_api
    item = waiting_review(work_api)
    body = review_resolution(item)
    uri = f"/api/v1/agent/work/{item['id']}"
    assert client.get(uri).status_code == 401
    assert client.post(uri + "/resolve", json=body).status_code == 401
    assert (
        client.get(
            uri, headers=headers() | {"Origin": "https://example.test"}
        ).status_code
        == 401
    )
    other = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Workspace(id=other, slug=f"foreign-review-{other}", name="Foreign"))
        session.flush()
        foreign = enqueue_work(
            session, workspace_id=other, source_key=str(uuid4()), kind="proposal_review"
        )
    try:
        assert (
            client.get(f"/api/v1/agent/work/{foreign}", headers=headers()).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/agent/work/{foreign}/resolve", json=body, headers=headers()
            ).status_code
            == 409
        )
    finally:
        cleanup_workspace(engine, other)
    monkeypatch.setenv("CRM_AUTOMATION_SCOPES", "work:read")
    assert client.get(uri, headers=headers()).status_code == 200
    assert (
        client.post(uri + "/resolve", json=body, headers=headers()).status_code == 403
    )


def test_review_resolution_preserves_audit_and_cannot_change_suppressed_business_state(
    work_api,
):
    from uuid import UUID
    from src.crm.persistence.models import AuditEvent

    client, engine, workspace, lead_id = work_api
    item = waiting_review(work_api)
    body = review_resolution(item)
    with Session(engine) as session, session.begin():
        lead = session.get(Lead, lead_id)
        lead.stage = "not_a_fit"
        session.get(Contact, lead.contact_id).status = "inactive"
        session.flush()
        version = lead.version
    uri = f"/api/v1/agent/work/{item['id']}/resolve"
    forbidden = body | {
        "result": body["result"] | {"next_action": {"task_type": "email"}}
    }
    assert client.post(uri, json=forbidden, headers=headers()).status_code == 422
    response = client.post(uri, json=body, headers=headers())
    assert response.status_code == 200, response.text
    resolved = response.json()
    assert resolved["status"] == "completed" and resolved["result"] == body["result"]
    assert (
        resolved["result_hash"] != item["result_hash"]
        and resolved["resolution_id"] == body["resolution_id"]
    )
    with Session(engine) as session:
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace,
                AuditEvent.command_id == UUID(body["resolution_id"]),
            )
        )
        assert (
            audit.action == "agent.review_resolved"
            and str(audit.entity_id) == item["id"]
        )
        assert audit.details["previous_result"] == item["result"]
        assert audit.details["resolution_result"] == body["result"]
        assert audit.details["previous_result_hash"] == item["result_hash"]
        lead = session.get(Lead, lead_id)
        assert lead.version == version and lead.stage == "not_a_fit"
        assert session.get(Contact, lead.contact_id).status == "inactive"
        assert not list(
            session.scalars(select(Task).where(Task.workspace_id == workspace))
        )
        assert not list(
            session.scalars(select(Proposal).where(Proposal.workspace_id == workspace))
        )


def test_review_resolution_replay_requires_same_uuid_target_and_payload(work_api):
    from src.crm.persistence.models import AuditEvent

    client, engine, workspace, _ = work_api
    item = waiting_review(work_api)
    body = review_resolution(item)
    uri = f"/api/v1/agent/work/{item['id']}/resolve"
    first = client.post(uri, json=body, headers=headers())
    replay = client.post(uri, json=body, headers=headers())
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json() | {"replayed": True}
    changed = body | {"result": body["result"] | {"summary": "Different resolution"}}
    assert client.post(uri, json=changed, headers=headers()).status_code == 409
    another = waiting_review(work_api)
    assert (
        client.post(
            f"/api/v1/agent/work/{another['id']}/resolve", json=body, headers=headers()
        ).status_code
        == 409
    )
    with Session(engine) as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(AuditEvent).where(AuditEvent.workspace_id == workspace)
                    )
                )
            )
            == 1
        )


def test_review_resolution_rejects_stale_hash_and_requires_new_evidence(work_api):
    client, _, _, _ = work_api
    item = waiting_review(work_api)
    body = review_resolution(item)
    uri = f"/api/v1/agent/work/{item['id']}/resolve"
    assert (
        client.post(
            uri, json=body | {"expected_result_hash": "0" * 64}, headers=headers()
        ).status_code
        == 409
    )
    assert (
        client.post(
            uri,
            json=body
            | {
                "result": {
                    "summary": "Reviewed",
                    "evidence": item["result"]["evidence"],
                }
            },
            headers=headers(),
        ).status_code
        == 409
    )
    assert (
        client.post(
            uri,
            json=body | {"result": {"summary": "Reviewed", "evidence": [{}]}},
            headers=headers(),
        ).status_code
        == 409
    )
    assert (
        client.post(
            uri,
            json=body | {"result": {"summary": "Reviewed", "evidence": []}},
            headers=headers(),
        ).status_code
        == 422
    )
    assert (
        client.get(uri.removesuffix("/resolve"), headers=headers()).json()["status"]
        == "waiting"
    )


@pytest.mark.parametrize(
    "kind,status",
    [
        ("calendar_callback", "waiting"),
        ("calendar_review", "waiting"),
        ("call_followup", "waiting"),
        ("followup_due", "waiting"),
        ("proposal_review", "queued"),
        ("proposal_review", "running"),
        ("proposal_review", "completed"),
        ("proposal_review", "failed"),
    ],
)
def test_review_resolution_rejects_nonreview_and_nonwaiting_items(
    work_api, kind, status
):
    from uuid import UUID

    client, engine, _, _ = work_api
    item = waiting_review(work_api, kind=kind)
    body = review_resolution(item)
    with Session(engine) as session, session.begin():
        row = session.get(AgentWork, UUID(item["id"]))
        row.status = status
        if status == "running":
            row.lease_token, row.lease_until = (
                uuid4(),
                datetime.now(UTC) + timedelta(minutes=1),
            )
    response = client.post(
        f"/api/v1/agent/work/{item['id']}/resolve", json=body, headers=headers()
    )
    assert response.status_code == 409


@pytest.mark.parametrize("same_resolution", [False, True])
def test_concurrent_review_resolution_has_one_audited_winner(work_api, same_resolution):
    from src.crm.persistence.models import AuditEvent

    client, engine, workspace, _ = work_api
    item = waiting_review(work_api)
    first = review_resolution(item)
    second = first if same_resolution else first | {"resolution_id": str(uuid4())}

    def resolve(body):
        return client.post(
            f"/api/v1/agent/work/{item['id']}/resolve", json=body, headers=headers()
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(resolve, [first, second]))
    assert sorted(response.status_code for response in responses) == (
        [200, 200] if same_resolution else [200, 409]
    )
    if same_resolution:
        assert sorted(response.json()["replayed"] for response in responses) == [
            False,
            True,
        ]
    with Session(engine) as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(AuditEvent).where(AuditEvent.workspace_id == workspace)
                    )
                )
            )
            == 1
        )


def test_review_resolution_keeps_long_evidence_lossless_in_bounded_audit_parts(
    work_api,
):
    import base64, json
    from src.crm.persistence.models import AuditEvent

    client, engine, workspace, _ = work_api
    old = {"summary": "ç" * 1200, "evidence": [{"source_text": "x" * 2000}]}
    item = waiting_review(work_api, result=old)
    body = review_resolution(item)
    body["result"] = {
        "summary": "New review",
        "evidence": [{"verified_text": "y" * 2500}],
    }
    response = client.post(
        f"/api/v1/agent/work/{item['id']}/resolve", json=body, headers=headers()
    )
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.workspace_id == workspace)
            )
        )
        main = next(row for row in rows if row.action == "agent.review_resolved")
        parts = sorted(
            (row for row in rows if row.action == "agent.review_resolution_evidence"),
            key=lambda row: row.details["part"],
        )
        assert len(parts) == main.details["history_parts"] > 1
        history = json.loads(
            base64.b64decode(
                "".join(row.details["history_json_base64"] for row in parts)
            )
        )
        assert history == {"previous_result": old, "resolution_result": body["result"]}
        assert all(
            len(json.dumps(row.details, ensure_ascii=False).encode()) <= 4096
            for row in rows
        )
