"""Protected KPI sources and human correction; never a new public writer."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from dashboard.app.feature_flags import require_postgres_command_writer
from dashboard.app.routers import accounts
from dashboard.app.routers.agent_events import _consume_rate_limit
from dashboard.app.routers.agent_work import (
    AutomationPrincipal,
    require_automation_principal,
    require_scope,
)
from dashboard.app.schemas.commercial_kpis import (
    AssessmentCommand,
    CorrectionCommand,
    ReconciliationCommand,
)
from dashboard.app.security import (
    CRMPrincipal,
    require_crm_principal,
    require_note_write_command_access,
)
from src.crm.persistence.models import AuditEvent
from src.crm.services import commercial_kpi_reconcile as reconciliation
from src.crm.services.commercial_kpi_service import (
    KPIConflict,
    SourceIndex,
    assess,
    correct,
    decode_cursor,
    effective,
    encode_cursor,
    period_bounds,
    semantic_hash,
)

router = APIRouter()
automation_router = APIRouter(prefix="/api/v1/agent/commercial-kpis")
BASE = "/api/v1/pipeline/call-metrics/activities"


def private_principal(
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
):
    if principal.subject == "public-browser":
        raise HTTPException(
            401,
            "Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="CRM"'},
        )
    if "crm:read" not in principal.permissions:
        raise HTTPException(403, "Forbidden")
    if not _consume_rate_limit(principal.workspace_id, "commercial-kpi-browser"):
        raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
    return principal


async def private_write(
    request: Request, principal: Annotated[CRMPrincipal, Depends(private_principal)]
):
    return await require_note_write_command_access(request, principal)


def read_context(principal: Annotated[CRMPrincipal, Depends(private_principal)]):
    yield from accounts.get_account_request_context(principal)


async def command_body(request, schema):
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > 16384:
            raise HTTPException(422, "Commercial KPI command exceeds 16 KiB")
    try:
        return schema.model_validate_json(bytes(chunks))
    except ValueError:
        raise HTTPException(422, "Invalid commercial KPI command") from None


def detail(session, workspace, activity_id):
    index = SourceIndex(session, workspace)
    try:
        source = index.context(activity_id)
    except KeyError:
        raise HTTPException(404, "Source not found") from None
    value = effective(session, workspace, activity_id, index=index)
    value.setdefault("override_revision", 0)
    return dict(
        value,
        source=source,
        last_failure=index.failed_inputs.get(
            semantic_hash(
                [
                    source[key]
                    for key in ("activity_id", "source_digest", "context_digest")
                ]
            )
        ),
    )


@router.get(BASE + "/{activity_id}")
def source_detail(
    activity_id: UUID,
    context: Annotated[accounts.AccountRequestContext, Depends(read_context)],
):
    return detail(context.session, context.principal.workspace_id, activity_id)


@router.get(BASE)
def source_list(
    context: Annotated[accounts.AccountRequestContext, Depends(read_context)],
    work_date: Annotated[date, Query(alias="date")],
    period: Literal["day", "week", "month"] = "day",
    status: Literal[
        "current", "pending", "stale", "conflict", "excluded", "all"
    ] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    workspace = context.principal.workspace_id
    binding = {
        "workspace": str(workspace),
        "date": work_date.isoformat(),
        "period": period,
        "status": status,
        "limit": limit,
    }
    try:
        state = (
            decode_cursor(cursor)
            if cursor
            else {
                "binding": binding,
                "cutoff": datetime.now(UTC).isoformat(),
                "after": None,
            }
        )
        if state.get("binding") != binding:
            raise ValueError
        cutoff = datetime.fromisoformat(state["cutoff"])
        start, end = period_bounds(work_date, period)
        index = SourceIndex(context.session, workspace)
        items = []
        for row in sorted(index.activities.values(), key=lambda row: row.id):
            if (
                row.activity_type != "call"
                or row.created_at > cutoff
                or not start <= row.occurred_at < end
            ):
                continue
            source = index.context(row.id)
            projected = effective(context.session, workspace, row.id, index=index)
            projected.setdefault("override_revision", 0)
            freshness = (
                projected["assessment"]["freshness"]
                if projected["assessment"]
                else "pending"
            )
            excluded = source["eligibility"] == "excluded" or bool(
                projected["assessment"]
                and freshness == "current"
                and projected["assessment"]["eligibility"] == "excluded"
            )
            if status != "all" and status != ("excluded" if excluded else freshness):
                continue
            items.append(
                dict(
                    projected,
                    source={
                        key: val
                        for key, val in source.items()
                        if key not in {"history", "related_context"}
                    },
                )
            )
        manifest = semantic_hash(items)
        if cursor and state.get("manifest") != manifest:
            raise ValueError
        state["manifest"] = manifest
        remaining = [
            item
            for item in items
            if state["after"] is None or item["source"]["activity_id"] > state["after"]
        ]
        page = remaining[:limit]
        has_more = len(remaining) > limit
        state["after"] = page[-1]["source"]["activity_id"] if page else state["after"]
        return {
            "items": page,
            "cutoff": state["cutoff"],
            "has_more": has_more,
            "next_cursor": encode_cursor(state) if has_more else None,
        }
    except (ValueError, KeyError):
        raise HTTPException(400, "Invalid or changed cursor; restart read") from None


@router.post(BASE + "/{activity_id}/correction")
async def correction(
    activity_id: UUID,
    request: Request,
    principal: Annotated[CRMPrincipal, Depends(private_write)],
):
    body = await command_body(request, CorrectionCommand)
    require_postgres_command_writer()
    args = body.model_dump(mode="json")
    args["command_id"] = body.command_id
    if request.headers.get("idempotency-key", str(body.command_id)) != str(
        body.command_id
    ):
        raise HTTPException(409, "Command conflict")
    try:
        with Session(accounts._account_engine()) as session, session.begin():
            result = correct(
                session, principal.workspace_id, principal.actor_id, activity_id, **args
            )
        return JSONResponse(result, status_code=200 if result["replayed"] else 201)
    except NoResultFound:
        raise HTTPException(404, "Source not found") from None
    except KPIConflict:
        raise HTTPException(409, "Source or command conflict") from None
    except ValueError:
        raise HTTPException(422, "Invalid commercial KPI correction") from None


def automation_read(
    principal: Annotated[AutomationPrincipal, Depends(require_automation_principal)],
):
    require_scope(principal, "work:read")
    return principal


def automation_write(
    principal: Annotated[AutomationPrincipal, Depends(require_automation_principal)],
):
    require_scope(principal, "work:write")
    return principal


@automation_router.get("/sources")
def automation_sources(
    principal: Annotated[AutomationPrincipal, Depends(automation_read)],
    cursor: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    try:
        with Session(accounts._account_engine()) as session:
            result = reconciliation.inventory_page(
                session, principal.workspace_id, cursor=cursor, limit=limit
            )
            for item in result["items"]:
                item["source"] = {
                    key: value
                    for key, value in item["source"].items()
                    if key
                    in {
                        "activity_id",
                        "source_digest",
                        "source_version",
                        "context_digest",
                        "canonical_company_id",
                        "eligibility",
                        "exclusion_reason",
                        "history_coverage",
                    }
                }
            return result
    except KPIConflict:
        raise HTTPException(409, "Inventory changed; restart sweep") from None
    except (ValueError, KeyError):
        raise HTTPException(400, "Invalid cursor") from None


@automation_router.get("/sources/{activity_id}")
def automation_source(
    activity_id: UUID,
    principal: Annotated[AutomationPrincipal, Depends(automation_read)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with Session(accounts._account_engine()) as session:
        value = detail(session, principal.workspace_id, activity_id)
        source = value["source"]
        items = [{"kind": "phone", "source": item} for item in source.pop("history")]
        items += [
            {"kind": "related", "source": item}
            for item in source.pop("related_context")
        ]
        binding = {
            "kind": "commercial-context",
            "workspace": str(principal.workspace_id),
            "activity_id": str(activity_id),
            "source_digest": source["source_digest"],
            "context_digest": source["context_digest"],
            "limit": limit,
        }
        try:
            state = (
                decode_cursor(cursor)
                if cursor
                else {
                    "binding": binding,
                    "after": 0,
                    "cutoff": datetime.now(UTC).isoformat(),
                }
            )
            if state["binding"] != binding:
                raise ValueError
            page = items[state["after"] : state["after"] + limit]
            state["after"] += len(page)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Invalid or changed context cursor") from None
        more = state["after"] < len(items)
        return dict(
            value,
            items=page,
            cutoff=state["cutoff"],
            has_more=more,
            next_cursor=encode_cursor(state) if more else None,
            processing=effective(
                session, principal.workspace_id, activity_id, inference_only=True
            ),
        )


@automation_router.post("/assessments")
async def automation_assess(
    request: Request,
    principal: Annotated[AutomationPrincipal, Depends(automation_write)],
):
    body = await command_body(request, AssessmentCommand)
    try:
        with Session(accounts._account_engine()) as session, session.begin():
            result = assess(
                session,
                principal.workspace_id,
                principal.actor_id,
                command_id=body.command_id,
                expected_source_digest=body.expected_source_digest,
                expected_context_digest=body.expected_context_digest,
                proposal=body.assessment.model_dump(mode="json"),
            )
        return JSONResponse(result, status_code=200 if result["replayed"] else 201)
    except NoResultFound:
        raise HTTPException(404, "Source not found") from None
    except KPIConflict:
        raise HTTPException(409, "Source or command conflict") from None
    except ValueError:
        raise HTTPException(422, "Invalid assessment") from None


@automation_router.post("/reconciliations")
async def automation_reconcile(
    request: Request,
    principal: Annotated[AutomationPrincipal, Depends(automation_write)],
):
    body = (await command_body(request, ReconciliationCommand)).root
    try:
        with Session(accounts._account_engine()) as session, session.begin():
            if body.operation == "start":
                result = reconciliation.start_run(
                    session,
                    principal.workspace_id,
                    principal.actor_id,
                    run_id=body.run_id,
                    entrypoint=body.entrypoint,
                    anchor_date=body.anchor_date,
                    expected_checkpoint=body.expected_checkpoint,
                )
            elif body.operation == "ack_page":
                result = reconciliation.acknowledge_page(
                    session,
                    principal.workspace_id,
                    principal.actor_id,
                    run_id=body.run_id,
                    page_token=body.page_token,
                    receipt_ids=body.receipt_ids,
                    expected_checkpoint=body.expected_checkpoint,
                )
            else:
                run = reconciliation.start_record(
                    session, principal.workspace_id, body.run_id
                )
                if (
                    body.entrypoint != run["entrypoint"]
                    or body.anchor_date.isoformat() != run["anchor_date"]
                ):
                    raise KPIConflict("Run mismatch")
                try:
                    result = reconciliation.finish_run(
                        session,
                        principal.workspace_id,
                        principal.actor_id,
                        run_id=body.run_id,
                        expected_checkpoint=body.expected_checkpoint,
                    )
                except reconciliation.KPIIncomplete:
                    result = reconciliation.fail_run(
                        session,
                        principal.workspace_id,
                        principal.actor_id,
                        run_id=body.run_id,
                        expected_checkpoint=body.expected_checkpoint,
                    )
        return result
    except KPIConflict:
        raise HTTPException(409, "Reconciliation or checkpoint conflict") from None
    except ValueError:
        raise HTTPException(422, "Invalid reconciliation") from None


@automation_router.get("/reconciliations/{run_id}")
def automation_run(
    run_id: UUID, principal: Annotated[AutomationPrincipal, Depends(automation_read)]
):
    with Session(accounts._account_engine()) as session:
        final = reconciliation.audit_by_command(
            session, principal.workspace_id, uuid5(run_id, "commercial-kpi-finish")
        )
        if final is not None:
            return final.details
        failed = session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == principal.workspace_id,
                AuditEvent.entity_type == "workspace",
                AuditEvent.entity_id == principal.workspace_id,
                AuditEvent.action == "commercial_kpi.reconciled",
                AuditEvent.details["run_id"].astext == str(run_id),
                AuditEvent.details["status"].astext == "failed",
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        if failed is not None:
            return failed.details
        try:
            run = reconciliation.start_record(session, principal.workspace_id, run_id)
        except KPIConflict:
            raise HTTPException(404, "Reconciliation not found") from None
        return {
            "run_id": str(run_id),
            "status": "running",
            "cutoff": run["cutoff"],
            "checkpoint_version": run["checkpoint_version"],
        }


router.include_router(automation_router)
