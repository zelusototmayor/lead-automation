"""Existing canonical HTTP saves must invalidate KPI reads before any worker."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.crm.persistence.models import AuditEvent
from src.crm.services.commercial_kpi_service import SourceIndex, assess
from tests.integration.api import test_lead_operations_api as _api_fixtures
from tests.integration.api.test_commercial_kpi_service import proposal, source
from tests.integration.api.test_lead_operations_api import (
    _headers,
)

lead_operations_api = _api_fixtures.lead_operations_api


def test_note_save_then_get_invalidates_before_worker_and_preserves_legacy(
    lead_operations_api, monkeypatch
):
    from dashboard.app.routers import accounts

    client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        from src.crm.persistence.models import Lead

        lead_row = session.get(Lead, lead)
        call = source(
            session,
            workspace,
            lead,
            occurred_at=now,
            account_id=lead_row.account_id,
            contact_id=lead_row.contact_id,
            summary="Operations manager explained the process and declined due to a contract.",
        )
        ctx = SourceIndex(session, workspace).context(call.id)
        assess(
            session,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=ctx["source_digest"],
            expected_context_digest=ctx["context_digest"],
            proposal=proposal(ctx),
        )
    url = f"/api/v1/pipeline/call-metrics?date={now.date().isoformat()}"
    before = client.get(url).json()
    assert before["commercial_kpis_v1"]["relevance"]["confirmed"] == 1
    command = uuid4()
    saved = client.post(
        f"/api/v1/commands/leads/{lead}/add-note",
        headers=_headers(command),
        json={
            "command_id": str(command),
            "expected_version": 1,
            "summary": "Correction to the prior call note: only reception answered; no substantive discussion.",
        },
    )
    assert saved.status_code == 200, saved.text
    with Session(engine) as session:
        audits_before = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.workspace_id == workspace)
        )
    after = client.get(url).json()
    assert after["commercial_kpis_v1"]["relevance"]["confirmed"] == 0
    assert after["commercial_kpis_v1"]["relevance"]["stale"] == 1
    assert after["counts"] == before["counts"]
    for period in ("week", "month"):
        body = client.get(url + f"&period={period}").json()
        for key in (
            "schema_version",
            "date",
            "counts",
            "coverage",
            "contact_counts",
            "target_first_answered",
        ):
            assert body[key] == after[key]
        assert body["commercial_kpis_v1"]["period"]["kind"] == period
    assert client.get(url + "&period=rolling7").status_code == 422
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.workspace_id == workspace)
            )
            == audits_before
        )


def test_protected_detail_correction_actual_readback_and_public_denial(
    lead_operations_api, monkeypatch
):
    from dashboard.app import main
    from dashboard.app.routers import accounts
    from dashboard.app.security import CRMPrincipal, require_crm_principal
    from src.crm.persistence.models import Lead

    client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    with Session(engine) as session, session.begin():
        lead_row = session.get(Lead, lead)
        call = source(
            session,
            workspace,
            lead,
            account_id=lead_row.account_id,
            contact_id=lead_row.contact_id,
            summary="Only reception scheduling.",
        )
        call_id = call.id
    url = f"/api/v1/pipeline/call-metrics/activities/{call_id}"
    detail = client.get(url)
    assert detail.status_code == 200, detail.text
    ctx = detail.json()["source"]
    correction = {
        key: value
        for key, value in proposal(ctx, "no").items()
        if key not in {"activity_id", "rule_version"}
    }
    command = uuid4()
    body = {
        "command_id": str(command),
        "expected_source_digest": ctx["source_digest"],
        "expected_context_digest": ctx["context_digest"],
        "expected_override_revision": 0,
        "operation": "set",
        "correction": correction,
    }
    saved = client.post(url + "/correction", json=body, headers=_headers(command))
    assert saved.status_code == 201, saved.text
    assert (
        client.post(
            url + "/correction", json=body, headers=_headers(command)
        ).status_code
        == 200
    )
    readback = client.get(url).json()
    assert (
        readback["assessment"]["relevant"] == "no"
        and readback["assessment"]["provenance"] == "human"
    )
    assert readback["source"]["summary"] == ctx["summary"]
    assert client.post(url + "/correction", json=body).status_code == 403
    assert (
        client.get(f"/api/v1/pipeline/call-metrics/activities/{uuid4()}").status_code
        == 404
    )
    public = CRMPrincipal(
        workspace_id=workspace,
        actor_id=actor,
        subject="public-browser",
        permissions=frozenset({"crm:read", "crm:note:write"}),
    )
    main.app.dependency_overrides[require_crm_principal] = lambda: public
    assert client.get(url).status_code == 401
    assert (
        client.post(
            url + "/correction", json=body, headers=_headers(command)
        ).status_code
        == 401
    )
    aggregate_body = client.get("/api/v1/pipeline/call-metrics?date=2026-09-15").json()[
        "commercial_kpis_v1"
    ]
    import json

    assert "Only reception" not in json.dumps(aggregate_body)
    assert str(call_id) not in json.dumps(aggregate_body)


def test_detail_inventory_exceeds_100_and_rejects_tampered_or_rebound_cursor(
    lead_operations_api, monkeypatch
):
    from dashboard.app.routers import accounts
    from src.crm.persistence.models import Lead

    client, engine, workspace, lead, _actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    with Session(engine) as session, session.begin():
        lead_row = session.get(Lead, lead)
        lead_row.stage = "lost"
        session.flush()
        expected = {
            str(
                source(
                    session,
                    workspace,
                    lead,
                    account_id=lead_row.account_id,
                    contact_id=lead_row.contact_id,
                ).id
            )
            for _ in range(105)
        }
    base = (
        "/api/v1/pipeline/call-metrics/activities?date=2026-09-15&period=day&limit=100"
    )
    response = client.get(base)
    assert response.status_code == 200, response.text
    page = response.json()
    assert len(page["items"]) == 100 and page["has_more"] is True
    cursor = page["next_cursor"]
    next_response = client.get(base + "&cursor=" + cursor)
    assert next_response.status_code == 200, (
        str(next_response.url),
        next_response.text,
    )
    next_page = next_response.json()
    assert next_page["has_more"] is False and next_page["next_cursor"] is None
    actual = [
        item["source"]["activity_id"] for item in page["items"] + next_page["items"]
    ]
    assert len(actual) == len(set(actual)) == len(expected) and set(actual) == expected
    assert client.get(base + "&cursor=x" + cursor[1:]).status_code == 400
    assert (
        client.get(
            base.replace("period=day", "period=month") + "&cursor=" + cursor
        ).status_code
        == 400
    )
