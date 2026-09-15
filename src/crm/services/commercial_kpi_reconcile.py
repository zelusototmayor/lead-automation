"""Bounded page receipts and a single leased checkpoint, with no model or Sales I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.crm.domain.commercial_kpi_contract import RULE, bounded_audit_details
from src.crm.persistence.models import AuditEvent, SyncCheckpoint
from src.crm.services.commercial_kpi_service import (
    KPIConflict,
    SourceIndex,
    chain_head,
    decode_cursor,
    effective,
    encode_cursor,
    period_bounds,
    semantic_hash,
)

CONNECTOR = "crm-commercial-kpis"
PAGE_SIZE = 100


class KPIIncomplete(KPIConflict):
    """A run can record failure without advancing its successful watermark."""


def checkpoint(session, workspace):
    session.execute(
        insert(SyncCheckpoint)
        .values(
            workspace_id=workspace,
            connector=CONNECTOR,
            source_scope=str(workspace),
            stream=RULE,
        )
        .on_conflict_do_nothing()
    )
    return session.scalar(
        select(SyncCheckpoint)
        .where(
            SyncCheckpoint.workspace_id == workspace,
            SyncCheckpoint.connector == CONNECTOR,
            SyncCheckpoint.source_scope == str(workspace),
            SyncCheckpoint.stream == RULE,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def checkpoint_version(row):
    return semantic_hash(
        {
            "workspace": str(row.workspace_id),
            "connector": CONNECTOR,
            "rule": RULE,
            "watermark": row.high_watermark_at.isoformat()
            if row.high_watermark_at
            else None,
            "success": row.last_success_at.isoformat() if row.last_success_at else None,
        }
    )


def audit_by_command(session, workspace, command):
    return session.scalar(
        select(AuditEvent).where(
            AuditEvent.workspace_id == workspace, AuditEvent.command_id == command
        )
    )


def start_record(session, workspace, run_id):
    row = audit_by_command(session, workspace, run_id)
    if (
        row is None
        or row.action != "commercial_kpi.reconcile_page"
        or row.details.get("operation") != "start"
    ):
        raise KPIConflict("Unknown reconciliation")
    return row.details


def inventory(session, workspace, run):
    index = SourceIndex(session, workspace)
    cutoff = datetime.fromisoformat(run["cutoff"])
    start, end = (
        datetime.fromisoformat(run["coverage_from"]),
        datetime.fromisoformat(run["coverage_through"]),
    )
    items = []
    for row in sorted(index.activities.values(), key=lambda row: row.id):
        if (
            row.activity_type == "call"
            and row.created_at <= cutoff
            and start <= row.occurred_at < end
        ):
            items.append(
                dict(
                    source=index.context(row.id),
                    **effective(session, workspace, row.id, index=index),
                )
            )
    return items


def input_manifest(items):
    return [
        [
            item["source"][key]
            for key in ("activity_id", "source_digest", "context_digest")
        ]
        for item in items
    ]


def inventory_cursor(workspace, run, items, after=None):
    return encode_cursor(
        {
            "kind": "commercial-inventory",
            "workspace": str(workspace),
            "run_id": run["run_id"],
            "cutoff": run["cutoff"],
            "after": after,
            "manifest": semantic_hash(input_manifest(items)),
        }
    )


def page_token(run, items):
    return semantic_hash(
        {
            "run_id": run["run_id"],
            "cutoff": run["cutoff"],
            "inputs": input_manifest(items),
        }
    )


def start_run(
    session, workspace, actor, *, run_id, entrypoint, anchor_date, expected_checkpoint
):
    if entrypoint not in {"sales_13h", "sales_1730"}:
        raise ValueError("Invalid entrypoint")
    row = checkpoint(session, workspace)
    version = checkpoint_version(row)
    now = datetime.now(UTC)
    existing = audit_by_command(session, workspace, run_id)
    if existing is not None:
        run = start_record(session, workspace, run_id)
        if (
            run["entrypoint"] != entrypoint
            or run["anchor_date"] != anchor_date.isoformat()
        ):
            raise KPIConflict("Run key conflict")
        final = audit_by_command(
            session, workspace, uuid5(run_id, "commercial-kpi-finish")
        )
        if final is not None:
            return final.details
        if run["checkpoint_version"] != version:
            raise KPIConflict("Superseded run")
    if (
        row.lease_owner
        and row.lease_expires_at > now
        and row.lease_owner != str(run_id)
    ):
        raise KPIConflict("Reconciliation already leased")
    if existing is None:
        if expected_checkpoint is not None and expected_checkpoint != version:
            raise KPIConflict("Checkpoint conflict")
        start, end = period_bounds(anchor_date, "month")
        run = {
            "rule_version": RULE,
            "operation": "start",
            "run_id": str(run_id),
            "entrypoint": entrypoint,
            "mode": "classification_only",
            "anchor_date": anchor_date.isoformat(),
            "started_at": now.isoformat(),
            "cutoff": now.isoformat(),
            "checkpoint_version": version,
            "coverage_from": start.isoformat(),
            "coverage_through": end.isoformat(),
        }
        session.add(
            AuditEvent(
                id=uuid4(),
                workspace_id=workspace,
                command_id=run_id,
                actor_id=actor,
                action="commercial_kpi.reconcile_page",
                entity_type="workspace",
                entity_id=workspace,
                details=bounded_audit_details(run),
            )
        )
        session.flush()
    else:
        run = start_record(session, workspace, run_id)
        if (
            run["entrypoint"] != entrypoint
            or run["anchor_date"] != anchor_date.isoformat()
        ):
            raise KPIConflict("Run key conflict")
    row.lease_owner = str(run_id)
    row.lease_expires_at = now + timedelta(minutes=5)
    items = inventory(session, workspace, run)
    return {
        "run_id": str(run_id),
        "cutoff": run["cutoff"],
        "checkpoint_version": run["checkpoint_version"],
        "next_cursor": inventory_cursor(workspace, run, items),
    }


def inventory_page(session, workspace, *, cursor, limit=100):
    state = decode_cursor(cursor)
    if (
        state.get("kind") != "commercial-inventory"
        or state.get("workspace") != str(workspace)
        or limit != PAGE_SIZE
    ):
        raise ValueError("Cursor binding mismatch")
    run = start_record(session, workspace, UUID(state["run_id"]))
    items = inventory(session, workspace, run)
    if (
        semantic_hash(input_manifest(items)) != state["manifest"]
        or state["cutoff"] != run["cutoff"]
    ):
        raise KPIConflict("Inventory changed; restart sweep")
    remaining = [
        item
        for item in items
        if state["after"] is None or item["source"]["activity_id"] > state["after"]
    ]
    page = remaining[:PAGE_SIZE]
    more = len(remaining) > PAGE_SIZE
    return {
        "items": page,
        "page_token": page_token(run, page),
        "cutoff": run["cutoff"],
        "has_more": more,
        "next_cursor": inventory_cursor(
            workspace, run, items, page[-1]["source"]["activity_id"]
        )
        if more
        else None,
    }


def current_receipts(session, workspace, page):
    receipts = []
    for item in page:
        source = item["source"]
        audit = chain_head(
            list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.workspace_id == workspace,
                        AuditEvent.entity_id == UUID(source["activity_id"]),
                        AuditEvent.entity_type == "activity",
                        AuditEvent.action == "commercial_kpi.assessed",
                    )
                )
            )
        )
        if (
            audit is None
            or any(
                audit.details["assessment"][key] != source[key]
                for key in ("source_digest", "source_version", "context_digest")
            )
            or audit.details.get("rule_version") != RULE
        ):
            raise KPIIncomplete("Page lacks current persisted assessments")
        receipts.append(audit)
    return receipts


def acknowledged_id(run_id, token):
    return uuid5(run_id, "commercial-kpi-page:" + token)


def require_lease(row, run_id, expected_checkpoint):
    if (
        checkpoint_version(row) != expected_checkpoint
        or row.lease_owner != str(run_id)
        or row.lease_expires_at is None
        or row.lease_expires_at <= datetime.now(UTC)
    ):
        raise KPIConflict("Checkpoint lease or version conflict")


def acknowledge_page(
    session, workspace, actor, *, run_id, page_token, receipt_ids, expected_checkpoint
):
    row = checkpoint(session, workspace)
    require_lease(row, run_id, expected_checkpoint)
    run = start_record(session, workspace, run_id)
    items = inventory(session, workspace, run)
    # Empty inventories still require one acknowledged empty page.
    pages = [
        items[pos : pos + PAGE_SIZE] for pos in range(0, len(items), PAGE_SIZE)
    ] or [[]]
    page = next(
        (page for page in pages if globals()["page_token"](run, page) == page_token),
        None,
    )
    if page is None:
        raise KPIConflict("Unknown or changed page")
    audits = current_receipts(session, workspace, page)
    expected = sorted(str(audit.id) for audit in audits)
    if sorted(str(receipt) for receipt in receipt_ids) != expected:
        raise KPIConflict("Incomplete or duplicated page readback")
    details = {
        "rule_version": RULE,
        "operation": "ack_page",
        "run_id": str(run_id),
        "page_token": page_token,
        "receipt_hash": semantic_hash(expected),
        "count": len(audits),
    }
    command = acknowledged_id(run_id, page_token)
    old = audit_by_command(session, workspace, command)
    if old is None:
        session.add(
            AuditEvent(
                id=uuid4(),
                workspace_id=workspace,
                command_id=command,
                actor_id=actor,
                action="commercial_kpi.reconcile_page",
                entity_type="workspace",
                entity_id=workspace,
                details=bounded_audit_details(details),
            )
        )
        session.flush()
    elif old.details != details:
        raise KPIConflict("Page receipt conflict")
    return details


def finish_run(session, workspace, actor, *, run_id, expected_checkpoint):
    final_id = uuid5(run_id, "commercial-kpi-finish")
    existing = audit_by_command(session, workspace, final_id)
    if existing is not None:
        return existing.details
    row = checkpoint(session, workspace)
    require_lease(row, run_id, expected_checkpoint)
    run = start_record(session, workspace, run_id)
    items = inventory(session, workspace, run)
    audits = []
    for pos in range(0, max(1, len(items)), PAGE_SIZE):
        page = items[pos : pos + PAGE_SIZE]
        token = page_token(run, page)
        receipt = audit_by_command(session, workspace, acknowledged_id(run_id, token))
        current = current_receipts(session, workspace, page)
        if receipt is None or receipt.details["receipt_hash"] != semantic_hash(
            sorted(str(audit.id) for audit in current)
        ):
            raise KPIIncomplete("Page readback not acknowledged")
        audits.extend(current)
    now = datetime.now(UTC)
    row.high_watermark_at = datetime.fromisoformat(run["cutoff"])
    row.last_success_at = now
    row.lease_owner = row.lease_expires_at = None
    row.last_error_redacted = None
    row.consecutive_failures = 0
    excluded = sum(
        item["source"]["eligibility"] == "excluded"
        or audit.details["assessment"]["eligibility"] == "excluded"
        for item, audit in zip(items, audits)
    )
    result = {
        "rule_version": RULE,
        "run_id": str(run_id),
        "entrypoint": run["entrypoint"],
        "mode": "classification_only",
        "started_at": run["started_at"],
        "finished_at": now.isoformat(),
        "cutoff": run["cutoff"],
        "input_watermark": semantic_hash(input_manifest(items)),
        "checkpoint_version": checkpoint_version(row),
        "coverage_from": run["coverage_from"],
        "coverage_through": run["coverage_through"],
        "discovered": len(items),
        "evaluated": sum(
            datetime.fromisoformat(
                audit.details["assessment"]["assessed_at"].replace("Z", "+00:00")
            )
            >= datetime.fromisoformat(run["started_at"])
            for audit in audits
        ),
        "current": len(items) - excluded,
        "unknown": sum(
            audit.details["assessment"]["relevant"] == "unknown"
            and item["source"]["eligibility"] != "excluded"
            and audit.details["assessment"]["eligibility"] != "excluded"
            for item, audit in zip(items, audits)
        ),
        "pending": 0,
        "failed": 0,
        "excluded": excluded,
        "status": "succeeded",
    }
    session.add(
        AuditEvent(
            id=uuid4(),
            workspace_id=workspace,
            command_id=final_id,
            actor_id=actor,
            action="commercial_kpi.reconciled",
            entity_type="workspace",
            entity_id=workspace,
            details=bounded_audit_details(result),
        )
    )
    session.flush()
    return result


def fail_run(session, workspace, actor, *, run_id, expected_checkpoint):
    """Finish an unsuccessful attempt; all unprocessed inputs remain retryable.

    `failed` means pending when this run failed, not a count of model invocations.
    Failure input hashes are paged independently so audit JSONB stays bounded.
    """
    row = checkpoint(session, workspace)
    require_lease(row, run_id, expected_checkpoint)
    run = start_record(session, workspace, run_id)
    items = inventory(session, workspace, run)
    missing, current = [], []
    for item in items:
        try:
            current.extend(current_receipts(session, workspace, [item]))
        except KPIConflict:
            missing.append(semantic_hash(input_manifest([item])[0]))
    input_hash = semantic_hash(input_manifest(items))
    command = uuid5(
        run_id,
        "commercial-kpi-failed:"
        + semantic_hash([input_hash, missing, sorted(str(a.id) for a in current)]),
    )
    old = audit_by_command(session, workspace, command)
    row.lease_owner = row.lease_expires_at = None
    if old is not None:
        return old.details
    now = datetime.now(UTC)
    excluded = sum(
        a.details["assessment"]["eligibility"] == "excluded" for a in current
    )
    receipt = {
        "rule_version": RULE,
        "run_id": str(run_id),
        "entrypoint": run["entrypoint"],
        "mode": "classification_only",
        "started_at": run["started_at"],
        "finished_at": now.isoformat(),
        "cutoff": run["cutoff"],
        "input_watermark": input_hash,
        "checkpoint_version": checkpoint_version(row),
        "coverage_from": run["coverage_from"],
        "coverage_through": run["coverage_through"],
        "discovered": len(items),
        "evaluated": sum(
            datetime.fromisoformat(
                a.details["assessment"]["assessed_at"].replace("Z", "+00:00")
            )
            >= datetime.fromisoformat(run["started_at"])
            for a in current
        ),
        "current": len(current) - excluded,
        "unknown": sum(
            a.details["assessment"]["relevant"] == "unknown"
            and a.details["assessment"]["eligibility"] == "eligible"
            for a in current
        ),
        "pending": len(missing),
        "failed": len(missing),
        "excluded": excluded,
        "status": "failed",
    }
    for pos in range(0, len(missing), 40):
        details = {
            "rule_version": RULE,
            "operation": "failed_inputs",
            "run_id": str(run_id),
            "failed_at": now.isoformat(),
            "code": "run_incomplete",
            "input_hashes": missing[pos : pos + 40],
        }
        session.add(
            AuditEvent(
                id=uuid4(),
                workspace_id=workspace,
                command_id=uuid5(command, f"failure-page:{pos}"),
                actor_id=actor,
                entity_type="workspace",
                entity_id=workspace,
                action="commercial_kpi.reconcile_page",
                details=bounded_audit_details(details),
            )
        )
    session.add(
        AuditEvent(
            id=uuid4(),
            workspace_id=workspace,
            command_id=command,
            actor_id=actor,
            entity_type="workspace",
            entity_id=workspace,
            action="commercial_kpi.reconciled",
            details=bounded_audit_details(receipt),
        )
    )
    row.last_error_redacted = (
        "Commercial KPI run incomplete; no successful watermark advanced"
    )
    row.consecutive_failures += 1
    session.flush()
    return receipt
