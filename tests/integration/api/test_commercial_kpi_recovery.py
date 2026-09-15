"""Restart and late-commit boundaries on real disposable PG/API transactions."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.crm.persistence.models import AuditEvent, Lead, SyncCheckpoint
from tests.integration.api import test_lead_operations_api as _api_fixtures
from tests.integration.api.test_commercial_kpi_model import load_client
from tests.integration.api.test_commercial_kpi_service import WHEN, proposal, source

lead_operations_api = _api_fixtures.lead_operations_api


@pytest.fixture
def recovery_api(lead_operations_api, monkeypatch):
    from dashboard.app.main import app
    from dashboard.app.routers import accounts
    from dashboard.app.routers.agent_work import (
        AutomationPrincipal,
        require_automation_principal,
    )

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
    return client, engine, workspace, lead, actor


def test_cursor_restart_retraverses_readbacks_reuses_committed_prefix(
    recovery_api, monkeypatch
):
    from src.crm.services import commercial_kpi_service as service

    client, engine, workspace, _, _ = recovery_api
    with Session(engine) as session, session.begin():
        # Separate companies keep this test about pagination, not quadratic history.
        for _ in range(101):
            lead = Lead(
                id=uuid4(), workspace_id=workspace, company_name="Synthetic restart"
            )
            session.add(lead)
            session.flush()
            source(session, workspace, lead.id, summary="Reception scheduling only.")

    module = load_client()
    trace, classified, prefix = [], [], set()

    class HTTP:
        restarted = False

        def call(self, method, path, payload=None):
            if (
                not self.restarted
                and path.startswith("/api/v1/agent/commercial-kpis/sources?")
                and prefix
            ):
                self.restarted = True
                # Exact volatile state lost on a new server process; all PG state survives.
                monkeypatch.setattr(
                    service, "_CURSOR_KEY", b"synthetic-restarted-process-key"
                )
                with Session(engine) as session:
                    checkpoint = session.scalar(
                        select(SyncCheckpoint).where(
                            SyncCheckpoint.workspace_id == workspace
                        )
                    )
                    assert checkpoint.high_watermark_at is None
                    assert checkpoint.last_success_at is None
            response = client.request(method, path, json=payload)
            trace.append((method, path.split("?")[0], payload, response.status_code))
            if response.status_code not in {200, 201}:
                # The existing transport's exact sanitized error contract.
                raise RuntimeError("CRM HTTP " + str(response.status_code))
            if payload and payload.get("operation") == "ack_page" and not prefix:
                prefix.update(payload["receipt_ids"])
                assert len(prefix) == 100
            return response.json()

    transport = module.KPITransport.__new__(module.KPITransport)
    transport.client = HTTP()

    def classifier(context):
        classified.append(context["activity_id"])
        return proposal(context, "no")

    result = module.run_reconciliation(
        transport,
        entrypoint="sales_13h",
        classification_only=True,
        anchor_date=WHEN.date(),
        classifier=classifier,
    )
    assert (
        result["status"] == "succeeded"
        and result["discovered"] == result["current"] == 101
    )
    assert len(classified) == len(set(classified)) == 101
    assert sum(status == 400 for _, _, _, status in trace) == 1
    assert (
        len(
            {
                body["run_id"]
                for _, _, body, _ in trace
                if body and body.get("operation") == "start"
            }
        )
        == 1
    )
    acks = [
        body for _, _, body, _ in trace if body and body.get("operation") == "ack_page"
    ]
    assert [len(body["receipt_ids"]) for body in acks] == [100, 100, 1]
    assert set(acks[1]["receipt_ids"]) == prefix
    with Session(engine) as session:
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace,
                    AuditEvent.action == "commercial_kpi.assessed",
                )
            )
        )
        assert len(audits) == 101 and prefix <= {str(a.id) for a in audits}
        assert all(a.details["revision"] == 1 for a in audits)
    cold = module.run_reconciliation(
        transport,
        entrypoint="sales_1730",
        classification_only=True,
        anchor_date=WHEN.date(),
        classifier=classifier,
    )
    assert (
        cold["evaluated"] == 0 and cold["discovered"] == 101 and len(classified) == 101
    )


@pytest.mark.parametrize("created_before_cutoff", [True, False])
def test_late_commit_backdate_never_skips_unread_input_and_next_sweep_catches_it(
    recovery_api, created_before_cutoff
):
    from src.crm.services.commercial_kpi_reconcile import (
        KPIIncomplete,
        acknowledge_page,
        finish_run,
        inventory_page,
        start_run,
    )
    from src.crm.services.commercial_kpi_service import KPIConflict, assess, effective

    _, engine, workspace, lead, actor = recovery_api
    with Session(engine) as setup, setup.begin():
        lead = uuid4()
        setup.add(
            Lead(id=lead, workspace_id=workspace, company_name="Synthetic late commit")
        )
        setup.flush()
        source(setup, workspace, lead, summary="Original recorded attempt.")
    with Session(engine) as scan, Session(engine) as late:
        # This separate uncommitted transaction is invisible to the first inventory.
        late_call = (
            source(
                late,
                workspace,
                lead,
                summary="Late backdated attempt.",
                occurred_at=WHEN - timedelta(days=1),
            )
            if created_before_cutoff
            else None
        )
        run_id = uuid4()
        start = start_run(
            scan,
            workspace,
            actor,
            run_id=run_id,
            entrypoint="sales_13h",
            anchor_date=WHEN.date(),
            expected_checkpoint=None,
        )
        scan.commit()
        page = inventory_page(scan, workspace, cursor=start["next_cursor"])
        assert len(page["items"]) == 1
        old_context = page["items"][0]["source"]
        receipt = assess(
            scan,
            workspace,
            actor,
            command_id=uuid4(),
            expected_source_digest=old_context["source_digest"],
            expected_context_digest=old_context["context_digest"],
            proposal=proposal(old_context, "no"),
        )
        scan.commit()
        assert (
            effective(scan, workspace, UUID(old_context["activity_id"]))["audit_id"]
            == receipt["audit_id"]
        )
        acknowledge_page(
            scan,
            workspace,
            actor,
            run_id=run_id,
            page_token=page["page_token"],
            receipt_ids=[UUID(receipt["audit_id"])],
            expected_checkpoint=start["checkpoint_version"],
        )
        scan.commit()
        if not created_before_cutoff:
            late_call = source(
                late,
                workspace,
                lead,
                summary="Late backdated attempt.",
                occurred_at=WHEN - timedelta(days=1),
            )
        assert late_call is not None
        assert (
            late_call.created_at <= datetime.fromisoformat(start["cutoff"])
        ) == created_before_cutoff
        late_id = late_call.id
        late.commit()  # AFTER inventory, assessment, readback and page acknowledgement.
        with pytest.raises(KPIConflict):
            inventory_page(scan, workspace, cursor=start["next_cursor"])
        with pytest.raises(KPIIncomplete):
            finish_run(
                scan,
                workspace,
                actor,
                run_id=run_id,
                expected_checkpoint=start["checkpoint_version"],
            )
        scan.rollback()
        checkpoint = scan.scalar(
            select(SyncCheckpoint).where(SyncCheckpoint.workspace_id == workspace)
        )
        assert (
            checkpoint.high_watermark_at is None and checkpoint.last_success_at is None
        )
        assert (
            effective(scan, workspace, UUID(old_context["activity_id"]))["assessment"][
                "freshness"
            ]
            == "stale"
        )
        # Resume the fixed cutoff: before-cutoff late commits enter it; after-cutoff
        # creations wait for the mandatory next full sweep, even with old occurrence.
        resumed = start_run(
            scan,
            workspace,
            actor,
            run_id=run_id,
            entrypoint="sales_13h",
            anchor_date=WHEN.date(),
            expected_checkpoint=start["checkpoint_version"],
        )
        scan.commit()
        fresh_page = inventory_page(scan, workspace, cursor=resumed["next_cursor"])
        assert len(fresh_page["items"]) == (2 if created_before_cutoff else 1)
        receipts = []
        for item in fresh_page["items"]:
            ctx = item["source"]
            saved = assess(
                scan,
                workspace,
                actor,
                command_id=uuid4(),
                expected_source_digest=ctx["source_digest"],
                expected_context_digest=ctx["context_digest"],
                proposal=proposal(ctx, "no"),
            )
            scan.commit()
            readback = effective(scan, workspace, UUID(ctx["activity_id"]))
            assert (
                readback["audit_id"] == saved["audit_id"]
                and readback["assessment"]["context_digest"] == ctx["context_digest"]
            )
            receipts.append(UUID(saved["audit_id"]))
        acknowledge_page(
            scan,
            workspace,
            actor,
            run_id=run_id,
            page_token=fresh_page["page_token"],
            receipt_ids=receipts,
            expected_checkpoint=start["checkpoint_version"],
        )
        first = finish_run(
            scan,
            workspace,
            actor,
            run_id=run_id,
            expected_checkpoint=start["checkpoint_version"],
        )
        scan.commit()
        assert first["discovered"] == len(fresh_page["items"])
        # New run always rescans below the watermark; created_at cannot skip this input.
        next_id = uuid4()
        next_run = start_run(
            scan,
            workspace,
            actor,
            run_id=next_id,
            entrypoint="sales_1730",
            anchor_date=WHEN.date(),
            expected_checkpoint=first["checkpoint_version"],
        )
        scan.commit()
        next_page = inventory_page(scan, workspace, cursor=next_run["next_cursor"])
        assert len(next_page["items"]) == 2
        assert str(late_id) in {
            item["source"]["activity_id"] for item in next_page["items"]
        }
        assert len({item["source"]["activity_id"] for item in next_page["items"]}) == 2
        # The next checkpoint also cannot advance until the late source is read back.
        with pytest.raises(KPIIncomplete):
            finish_run(
                scan,
                workspace,
                actor,
                run_id=next_id,
                expected_checkpoint=next_run["checkpoint_version"],
            )
        scan.rollback()
        receipts = []
        for item in next_page["items"]:
            ctx = item["source"]
            readback = effective(scan, workspace, UUID(ctx["activity_id"]))
            if readback["assessment"] is None:
                saved = assess(
                    scan,
                    workspace,
                    actor,
                    command_id=uuid4(),
                    expected_source_digest=ctx["source_digest"],
                    expected_context_digest=ctx["context_digest"],
                    proposal=proposal(ctx, "no"),
                )
                scan.commit()
                readback = effective(scan, workspace, UUID(ctx["activity_id"]))
                assert readback["audit_id"] == saved["audit_id"]
            assert readback["assessment"]["freshness"] == "current"
            assert readback["assessment"]["context_digest"] == ctx["context_digest"]
            receipts.append(UUID(readback["audit_id"]))
        acknowledge_page(
            scan,
            workspace,
            actor,
            run_id=next_id,
            page_token=next_page["page_token"],
            receipt_ids=receipts,
            expected_checkpoint=next_run["checkpoint_version"],
        )
        final = finish_run(
            scan,
            workspace,
            actor,
            run_id=next_id,
            expected_checkpoint=next_run["checkpoint_version"],
        )
        scan.commit()
        assert final["discovered"] == final["current"] == 2 and final["pending"] == 0
        assert final["evaluated"] == (0 if created_before_cutoff else 1)
        audits = list(
            scan.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace,
                    AuditEvent.action == "commercial_kpi.assessed",
                )
            )
        )
        assert len(audits) == 3  # original + source/history invalidation + late event
        assert len({a.entity_id for a in audits}) == 2


def test_client_restart_after_interruption_respects_lease_and_reuses_prefix(
    recovery_api,
):
    client, engine, workspace, _, _ = recovery_api
    with Session(engine) as session, session.begin():
        lead = uuid4()
        session.add(
            Lead(
                id=lead, workspace_id=workspace, company_name="Synthetic client restart"
            )
        )
        session.flush()
        for offset in range(3):
            source(
                session,
                workspace,
                lead,
                occurred_at=WHEN + timedelta(minutes=offset),
                summary="Reception only.",
            )

    class HTTP:
        def call(self, method, path, payload=None):
            response = client.request(method, path, json=payload)
            if response.status_code not in {200, 201}:
                raise RuntimeError("CRM HTTP " + str(response.status_code))
            return response.json()

    module = load_client()
    transport = module.KPITransport.__new__(module.KPITransport)
    transport.client = HTTP()
    seen = []

    def interrupted(context):
        if seen:
            raise SystemExit("Synthetic process interruption, no finish/ack")
        seen.append(context["activity_id"])
        return proposal(context, "no")

    with pytest.raises(SystemExit):
        module.run_reconciliation(
            transport,
            entrypoint="sales_13h",
            classification_only=True,
            anchor_date=WHEN.date(),
            classifier=interrupted,
        )
    # Recreate the client with no old in-memory cursors. A live lease is not stolen.
    restarted = load_client()
    transport = restarted.KPITransport.__new__(restarted.KPITransport)
    transport.client = HTTP()
    with pytest.raises(restarted.InventoryRestart):
        restarted.run_reconciliation(
            transport,
            entrypoint="sales_1730",
            classification_only=True,
            anchor_date=WHEN.date(),
            classifier=interrupted,
        )
    with Session(engine) as session, session.begin():
        checkpoint = session.scalar(
            select(SyncCheckpoint).where(SyncCheckpoint.workspace_id == workspace)
        )
        assert (
            checkpoint.high_watermark_at is None and checkpoint.last_success_at is None
        )
        checkpoint.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        prefix = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace,
                    AuditEvent.action == "commercial_kpi.assessed",
                )
            )
        )
        assert len(prefix) == 1
        prefix_id = prefix[0].id
    resumed = []

    def recovered(context):
        resumed.append(context["activity_id"])
        return proposal(context, "no")

    final = restarted.run_reconciliation(
        transport,
        entrypoint="sales_1730",
        classification_only=True,
        anchor_date=WHEN.date(),
        classifier=recovered,
    )
    assert (
        final["status"] == "succeeded" and final["current"] == final["discovered"] == 3
    )
    assert final["evaluated"] == len(resumed) == 2 and set(seen).isdisjoint(resumed)
    with Session(engine) as session:
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace,
                    AuditEvent.action == "commercial_kpi.assessed",
                )
            )
        )
        assert len(audits) == 3 and prefix_id in {a.id for a in audits}
