"""Full-sweep KPI receipts on disposable PostgreSQL; no external adapters."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from src.crm.persistence.models import SyncCheckpoint
from src.crm.services.commercial_kpi_service import (
    KPIConflict,
    SourceIndex,
    assess,
    effective,
)
from tests.integration.api import test_commercial_kpi_service as _kpi_fixtures
from tests.integration.api import test_lead_operations_api as _api_fixtures
from tests.integration.api.test_commercial_kpi_service import (
    WHEN,
    proposal,
    source,
)

lead_operations_api = _api_fixtures.lead_operations_api
kpi_db = _kpi_fixtures.kpi_db


def test_finish_requires_full_assessment_readback_and_page_ack(kpi_db):
    from src.crm.services.commercial_kpi_reconcile import (
        acknowledge_page,
        finish_run,
        inventory_page,
        start_run,
    )

    session, workspace, actor, lead = kpi_db
    prior = source(
        session,
        workspace,
        lead,
        occurred_at=WHEN - timedelta(days=40),
        outcome_code="no_answer",
    )
    calls = [
        source(
            session,
            workspace,
            lead,
            occurred_at=WHEN + timedelta(minutes=offset),
            summary="Only scheduling with reception.",
        )
        for offset in range(3)
    ]
    run_id = uuid4()
    start = start_run(
        session,
        workspace,
        actor,
        run_id=run_id,
        entrypoint="sales_13h",
        anchor_date=WHEN.date(),
        expected_checkpoint=None,
    )
    checkpoint = session.scalar(
        select(SyncCheckpoint).where(SyncCheckpoint.workspace_id == workspace)
    )
    assert checkpoint.last_success_at is None
    with pytest.raises(KPIConflict):
        finish_run(
            session,
            workspace,
            actor,
            run_id=run_id,
            expected_checkpoint=start["checkpoint_version"],
        )
    page = inventory_page(session, workspace, cursor=start["next_cursor"], limit=100)
    assert {item["source"]["activity_id"] for item in page["items"]} == {
        str(row.id) for row in calls
    }
    assert page["has_more"] is False
    with pytest.raises(KPIConflict):
        acknowledge_page(
            session,
            workspace,
            actor,
            run_id=run_id,
            page_token=page["page_token"],
            receipt_ids=[],
            expected_checkpoint=start["checkpoint_version"],
        )
    receipts = []
    for row in calls:
        context = SourceIndex(session, workspace).context(row.id)
        claim = proposal(context, "no")
        receipt = assess(
            session,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=context["source_digest"],
            expected_context_digest=context["context_digest"],
            proposal=claim,
        )
        readback = effective(session, workspace, row.id)
        assert readback["audit_id"] == receipt["audit_id"]
        assert readback["assessment"]["source_digest"] == context["source_digest"]
        receipts.append(UUID(receipt["audit_id"]))
    with pytest.raises(KPIConflict):
        acknowledge_page(
            session,
            workspace,
            actor,
            run_id=run_id,
            page_token=page["page_token"],
            receipt_ids=receipts[:-1],
            expected_checkpoint=start["checkpoint_version"],
        )
    acknowledge_page(
        session,
        workspace,
        actor,
        run_id=run_id,
        page_token=page["page_token"],
        receipt_ids=receipts,
        expected_checkpoint=start["checkpoint_version"],
    )
    result = finish_run(
        session,
        workspace,
        actor,
        run_id=run_id,
        expected_checkpoint=start["checkpoint_version"],
    )
    assert result["status"] == "succeeded"
    assert (
        result["discovered"] == result["evaluated"] == result["current"] == len(calls)
    )
    assert result["pending"] == result["failed"] == 0
    assert (
        checkpoint.last_success_at is not None and checkpoint.cursor_encrypted is None
    )
    assert effective(session, workspace, prior.id)["assessment"] is None
    assert (
        finish_run(
            session,
            workspace,
            actor,
            run_id=run_id,
            expected_checkpoint=start["checkpoint_version"],
        )
        == result
    )


def test_entrypoints_share_lease_and_expired_owner_cannot_advance(kpi_db):
    from src.crm.services.commercial_kpi_reconcile import (
        acknowledge_page,
        finish_run,
        inventory_page,
        start_run,
    )

    session, workspace, actor, _lead = kpi_db
    first, second = uuid4(), uuid4()
    start = start_run(
        session,
        workspace,
        actor,
        run_id=first,
        entrypoint="sales_13h",
        anchor_date=WHEN.date(),
        expected_checkpoint=None,
    )
    with pytest.raises(KPIConflict):
        start_run(
            session,
            workspace,
            actor,
            run_id=second,
            entrypoint="sales_1730",
            anchor_date=WHEN.date(),
            expected_checkpoint=None,
        )
    checkpoint = session.scalar(
        select(SyncCheckpoint).where(SyncCheckpoint.workspace_id == workspace)
    )
    checkpoint.lease_expires_at -= timedelta(hours=1)
    session.flush()
    later = start_run(
        session,
        workspace,
        actor,
        run_id=second,
        entrypoint="sales_1730",
        anchor_date=WHEN.date(),
        expected_checkpoint=None,
    )
    first_page = inventory_page(
        session, workspace, cursor=start["next_cursor"], limit=100
    )
    with pytest.raises(KPIConflict):
        acknowledge_page(
            session,
            workspace,
            actor,
            run_id=first,
            page_token=first_page["page_token"],
            receipt_ids=[],
            expected_checkpoint=start["checkpoint_version"],
        )
    with pytest.raises(KPIConflict):
        finish_run(
            session,
            workspace,
            actor,
            run_id=first,
            expected_checkpoint=start["checkpoint_version"],
        )
    page = inventory_page(session, workspace, cursor=later["next_cursor"], limit=100)
    acknowledge_page(
        session,
        workspace,
        actor,
        run_id=second,
        page_token=page["page_token"],
        receipt_ids=[],
        expected_checkpoint=later["checkpoint_version"],
    )
    result = finish_run(
        session,
        workspace,
        actor,
        run_id=second,
        expected_checkpoint=later["checkpoint_version"],
    )
    assert result["status"] == "succeeded" and result["discovered"] == 0
    assert (
        start_run(
            session,
            workspace,
            actor,
            run_id=second,
            entrypoint="sales_1730",
            anchor_date=WHEN.date(),
            expected_checkpoint=None,
        )["status"]
        == "succeeded"
    )
    assert checkpoint.lease_owner is None


@pytest.mark.parametrize("entrypoint", ["sales_13h", "sales_1730"])
def test_both_actual_classification_only_paths_http_readbacks_cold_repeat(
    lead_operations_api, monkeypatch, entrypoint
):
    from sqlalchemy.orm import Session

    from dashboard.app.main import app
    from dashboard.app.routers import accounts
    from dashboard.app.routers.agent_work import (
        AutomationPrincipal,
        require_automation_principal,
    )
    from src.crm.persistence.models import AuditEvent, Lead
    from tests.integration.api.test_commercial_kpi_model import load_client

    client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    app.dependency_overrides[require_automation_principal] = lambda: (
        AutomationPrincipal(
            workspace_id=workspace,
            actor_id=actor,
            scopes=frozenset({"work:read", "work:write"}),
            source_scopes=frozenset(),
        )
    )
    with Session(engine) as session, session.begin():
        lead_row = session.get(Lead, lead)
        lead_row.stage = "lost"
        session.flush()
        for _ in range(105):
            source(
                session,
                workspace,
                lead,
                account_id=lead_row.account_id,
                contact_id=lead_row.contact_id,
                summary="Reception scheduling only.",
            )
    trace = []

    class Transport:
        def call(self, method, path, payload=None):
            assert path.startswith("/api/v1/agent/commercial-kpis/")
            trace.append((method, path.split("?")[0]))
            response = client.request(method, path, json=payload)
            if response.status_code not in {200, 201}:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
            return response.json()

    module = load_client()
    evaluated = []

    def classifier(context):
        assert len(context["history"]) >= 104
        assert len({item["activity_id"] for item in context["history"]}) == len(
            context["history"]
        )
        evaluated.append(context["activity_id"])
        return proposal(context, "no")

    first = module.run_reconciliation(
        Transport(),
        entrypoint=entrypoint,
        classification_only=True,
        anchor_date=WHEN.date(),
        classifier=classifier,
    )
    assert (
        first["status"] == "succeeded"
        and first["discovered"] == first["current"] == len(evaluated) == 105
    )
    second = module.run_reconciliation(
        Transport(),
        entrypoint=entrypoint,
        classification_only=True,
        anchor_date=WHEN.date(),
        classifier=classifier,
    )
    assert (
        second["status"] == "succeeded"
        and second["evaluated"] == 0
        and len(evaluated) == 105
    )
    assessment_path = "/api/v1/agent/commercial-kpis/assessments"
    for position, (method, path) in enumerate(trace):
        if method == "POST" and path == assessment_path:
            assert (
                trace[position + 1][0] == "GET"
                and "/sources/" in trace[position + 1][1]
            )
    with Session(engine) as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.workspace_id == workspace,
                            AuditEvent.action == "commercial_kpi.assessed",
                        )
                    )
                )
            )
            == 105
        )


def test_model_failure_is_pending_failed_then_retry_reuses_committed_prefix(
    lead_operations_api, monkeypatch
):
    from sqlalchemy.orm import Session

    from dashboard.app.main import app
    from dashboard.app.routers import accounts
    from dashboard.app.routers.agent_work import (
        AutomationPrincipal,
        require_automation_principal,
    )
    from src.crm.persistence.models import AuditEvent, Lead
    from src.crm.services.commercial_kpi_service import aggregate
    from tests.integration.api.test_commercial_kpi_model import load_client

    client, engine, workspace, lead, actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    app.dependency_overrides[require_automation_principal] = lambda: (
        AutomationPrincipal(
            workspace_id=workspace,
            actor_id=actor,
            scopes=frozenset({"work:read", "work:write"}),
            source_scopes=frozenset(),
        )
    )
    with Session(engine) as session, session.begin():
        lead_row = session.get(Lead, lead)
        for offset in range(3):
            source(
                session,
                workspace,
                lead,
                account_id=lead_row.account_id,
                contact_id=lead_row.contact_id,
                occurred_at=WHEN + timedelta(minutes=offset),
                summary="Synthetic logistics only.",
            )

    class HTTP:
        last_run = None

        def call(self, method, path, payload=None):
            if method == "POST" and payload.get("operation") == "start":
                self.last_run = payload["run_id"]
            response = (
                client.request(method, path, json=payload)
                if payload is not None
                else client.get(path)
            )
            assert response.status_code in {200, 201}, (
                path,
                response.status_code,
                response.text,
            )
            return response.json()

    transport = HTTP()
    module = load_client()
    called = []

    def failing_classifier(context):
        called.append(context["activity_id"])
        if len(called) > 1:
            raise module.ModelUnavailable("Synthetic model failure")
        return proposal(context, relevant="no")

    with pytest.raises(module.ModelUnavailable):
        module.run_reconciliation(
            transport,
            entrypoint="sales_13h",
            classification_only=True,
            anchor_date=WHEN.date(),
            classifier=failing_classifier,
        )
    failed = client.get(
        "/api/v1/agent/commercial-kpis/reconciliations/" + transport.last_run
    ).json()
    assert failed["status"] == "failed" and failed["pending"] == failed["failed"] == 2
    with Session(engine) as session:
        counts = aggregate(
            session,
            workspace,
            anchor_date=WHEN.date(),
            period="month",
            legacy_anchor_day_attempts=3,
        )
        assert counts["coverage"]["processing"]["failed"] == 2
        assert (
            session.scalar(
                select(SyncCheckpoint).where(SyncCheckpoint.workspace_id == workspace)
            ).high_watermark_at
            is None
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.workspace_id == workspace,
                            AuditEvent.action == "commercial_kpi.assessed",
                        )
                    )
                )
            )
            == 1
        )
    reused = []

    def recovered(context):
        reused.append(context["activity_id"])
        return proposal(context, relevant="no")

    final = module.run_reconciliation(
        transport,
        entrypoint="sales_1730",
        classification_only=True,
        anchor_date=WHEN.date(),
        classifier=recovered,
    )
    assert (
        final["status"] == "succeeded" and final["evaluated"] == 2 and len(reused) == 2
    )
    with Session(engine) as session:
        counts = aggregate(
            session,
            workspace,
            anchor_date=WHEN.date(),
            period="month",
            legacy_anchor_day_attempts=3,
        )
        assert counts["coverage"]["processing"]["failed"] == 0
        assert counts["coverage"]["source"]["complete"] is True
        assert counts["coverage"]["last_run_id"] == final["run_id"]


@pytest.mark.parametrize("entrypoint", ["sales_13h", "sales_1730"])
def test_real_cli_entrypoint_against_disposable_loopback_api(
    lead_operations_api, monkeypatch, entrypoint
):
    import json
    import os
    import socket
    import subprocess
    import threading
    import time
    from pathlib import Path

    import uvicorn

    from dashboard.app.main import app
    from dashboard.app.routers import accounts

    _client, engine, workspace, _lead, _actor = lead_operations_api
    monkeypatch.setattr(accounts, "_account_engine", lambda: engine)
    monkeypatch.setenv(
        "CRM_AUTOMATION_BEARER_TOKEN", "synthetic-kpi-disposable-auth-000000000000"
    )
    monkeypatch.setenv("CRM_AUTOMATION_WORKSPACE_ID", str(workspace))
    monkeypatch.setenv("CRM_AUTOMATION_SCOPES", "work:read,work:write")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        env = dict(
            os.environ, CRM_AGENT_BASE_URL=f"http://127.0.0.1:{sock.getsockname()[1]}"
        )
        script = (
            Path(__file__).resolve().parents[3]
            / "ops/hourly-sales/commercial_kpi_reconcile.py"
        )
        result = subprocess.run(
            [
                "/Users/max/.hermes/hermes-agent/venv/bin/python",
                str(script),
                "--entrypoint",
                entrypoint,
                "--classification-only",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert (
            receipt["entrypoint"] == entrypoint
            and receipt["status"] == "succeeded"
            and receipt["discovered"] == 0
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
