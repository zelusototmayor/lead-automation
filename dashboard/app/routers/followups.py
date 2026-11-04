"""One additive ordinary followup API. No body carries receipt authority."""

import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from dashboard.app.routers.agent_events import _consume_rate_limit
from dashboard.app.routers.agent_work import Database, Principal, require_scope
from dashboard.app.schemas.followups import (
    AnchorPrepareBody,
    ExecuteBody,
    PayloadPrepareBody,
    ProjectionReconcileBody,
    ReconcileBody,
    RecoverBody,
    SentContactBody,
    SentContactReadback,
    SentContactReceipt,
    VerdictBody,
)
from src.crm.connectors.followup_email import ProviderReadError
from src.crm.connectors.gmail_cooldown import GmailRateLimited
from src.crm.services import followup_service as service
from src.crm.services.agent_work_service import WorkConflict
from src.crm.services import followup_preparation_recovery as preparation_recovery
from src.crm.services.followup_review import (
    FollowupError,
    find_request,
    request_projection,
    review_configuration,
    strict_json,
)


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def private(request):
            try:
                if request.method == "POST":
                    raw = await request.body()
                    strict_json(raw)
                return await handle(request)
            except FollowupError as exc:
                raise HTTPException(exc.status, exc.code) from None
            except GmailRateLimited as exc:
                raise HTTPException(429, exc.reason_code, headers=exc.headers) from None
            except ProviderReadError:
                raise HTTPException(502, "provider_read_failed") from None
            except WorkConflict:
                raise HTTPException(409, "work_context_conflict") from None
            except RequestValidationError:
                raise HTTPException(422, "invalid_followup_schema") from None
            except ValueError:
                raise HTTPException(422, "invalid_followup_data") from None

        return private


router = APIRouter(prefix="/api/v1/agent", route_class=PrivateRoute)


def get_followup_provider(request: Request):
    if request.url.path.endswith("/followups/audit") and request.query_params.get("view", "crm") in {"crm", "continuity"}:
        return None  # The default inventory remains CRM-only, no Gmail capability.
    from src.crm.connectors.followup_email import GmailProvider
    credentials = os.environ.get("GOOGLE_GMAIL_CREDENTIALS_FILE")
    if not credentials:
        raise FollowupError("gmail_capability_missing", 503)
    return GmailProvider(credentials_file=credentials)


Provider = Annotated[object, Depends(get_followup_provider)]


def require_followup_review_principal(request: Request):
    principal, token = review_configuration()
    authorization = request.headers.get("authorization", "")
    if authorization and authorization != "Bearer " + token:
        raise FollowupError("review_role_required", 403)
    try:
        issued = datetime.fromisoformat(
            request.headers.get("x-agent-timestamp", "").replace("Z", "+00:00")
        )
        if (
            not secrets.compare_digest(authorization, "Bearer " + token)
            or issued.tzinfo is None
            or abs(datetime.now(UTC) - issued) > timedelta(seconds=60)
            or request.url.scheme != "https"
            or request.headers.get("origin")
            or request.headers.get("cookie")
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise FollowupError("review_unauthorized", 401) from None
    if not _consume_rate_limit(principal.workspace_id, "followup-review"):
        raise FollowupError("review_rate_limited", 429)
    return principal


Reviewer = Annotated[object, Depends(require_followup_review_principal)]


@router.get("/followups/context/{lead_id}")
def context(lead_id: UUID, principal: Principal, session: Database, provider: Provider,
            anchor_message_id: Annotated[str | None, Query(min_length=1, max_length=255)] = None):
    require_scope(principal, "work:read")
    return service.current_context(
        session, principal, lead_id, os.environ.get("CRM_GOOGLE_MAILBOX", ""), provider,
        anchor_message_id=anchor_message_id,
    )


@router.get("/followups/projection/{lead_id}")
def projection(lead_id: UUID, principal: Principal, session: Database, provider: Provider):
    require_scope(principal, "work:read")
    from src.crm.services.followup_projection import query
    return query(session, principal, lead_id, os.environ.get("CRM_GOOGLE_MAILBOX", ""), provider)


@router.post("/followups/projection/{lead_id}/reconcile")
def reconcile_projection(lead_id: UUID, body: ProjectionReconcileBody,
                         principal: Principal, session: Database, provider: Provider):
    require_scope(principal, "work:write")
    from src.crm.services.followup_projection import reconcile
    with session.begin():
        return reconcile(session, principal, lead_id, os.environ.get("CRM_GOOGLE_MAILBOX", ""), provider, body)


@router.get("/followups/audit")
def followup_audit(
    principal: Principal,
    session: Database,
    provider: Provider,
    mailbox: str,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
    view: Literal["crm", "sent", "continuity"] = "crm",
):
    require_scope(principal, "work:read")
    if view == "continuity":
        from src.crm.services.followup_continuity import inventory
        return inventory(session, principal, mailbox, cursor=cursor, limit=limit)
    if view == "sent":
        from src.crm.services.followup_intake import sent_inventory

        return sent_inventory(session, principal, mailbox, provider, cursor=cursor, limit=limit)
    return service.audit_inventory(
        session, principal, mailbox, cursor=cursor, limit=limit
    )


@router.get('/followups/sent-contact/{lead_id}/{command_id}', response_model=SentContactReadback)
def sent_contact_readback(lead_id: UUID, command_id: UUID, principal: Principal, session: Database):
    from src.crm.services.sent_contact import readback
    require_scope(principal, 'work:read')
    return readback(session, principal, lead_id, command_id)


@router.post('/followups/sent-contact', response_model=SentContactReceipt)
def sent_contact(body: SentContactBody, principal: Principal, session: Database, provider: Provider):
    """Record one source-proven secondary contact, no commercial effects."""
    from sqlalchemy.exc import IntegrityError
    from src.crm.services.sent_contact import associate
    require_scope(principal, 'work:write')
    try:
        with session.begin():
            return associate(session, principal, body, provider)
    except IntegrityError:
        raise FollowupError('sent_contact_identity_conflict') from None


@router.post("/followups/reconcile")
def reconcile(
    body: ReconcileBody, principal: Principal, session: Database, provider: Provider
):
    require_scope(principal, "work:write")
    with session.begin():
        return service.reconcile(session, principal, body, provider)


@router.post("/work/{work_id}/followup/anchor/prepare")
def prepare_anchor(
    work_id: UUID,
    body: AnchorPrepareBody,
    principal: Principal,
    session: Database,
    provider: Provider,
    response: Response,
):
    require_scope(principal, "work:write")
    with session.begin():
        item = service.prepare_anchor(session, principal, work_id, body, provider)
        package = item["package"]
        response.status_code = 200 if item["status"] == "approved" else 202
        return {
            "work_id": str(work_id),
            "obligation_key": None,
            "disposition": "eligible"
            if item["status"] == "approved"
            else "pending_review",
            "reason_code": "review_approved"
            if item["status"] == "approved"
            else "review_requested",
            "context_hash": package["context_hash"],
            "payload_sha256": package["payload_sha256"],
            "review_request_id": package["request_id"],
            "reviewer_receipt_id": item["receipt"]["id"] if item["receipt"] else None,
            "expires_at": package["expires_at"],
        }


@router.post("/work/{work_id}/followup/prepare")
def prepare_payload(
    work_id: UUID,
    body: PayloadPrepareBody,
    principal: Principal,
    session: Database,
    provider: Provider,
    response: Response,
):
    require_scope(principal, "work:write")
    with session.begin():
        item = service.prepare_payload(session, principal, work_id, body, provider)
        package = item["package"]
        response.status_code = 200 if item["status"] == "approved" else 202
        return {
            "work_id": str(work_id),
            "obligation_key": package["obligation_key"],
            "disposition": "eligible"
            if item["status"] == "approved"
            else "pending_review",
            "reason_code": "review_approved"
            if item["status"] == "approved"
            else "review_requested",
            "context_hash": package["context_hash"],
            "payload_sha256": package["payload_sha256"],
            "review_request_id": package["request_id"],
            "reviewer_receipt_id": item["receipt"]["id"] if item["receipt"] else None,
            "expires_at": package["expires_at"],
        }


@router.post("/work/{work_id}/followup/execute")
def execute(
    work_id: UUID,
    body: ExecuteBody,
    principal: Principal,
    session: Database,
    provider: Provider,
):
    require_scope(principal, "followups:execute")
    return service.execute(session, principal, work_id, body, provider)


@router.post("/work/{work_id}/followup/recover")
def recover(
    work_id: UUID,
    body: RecoverBody,
    principal: Principal,
    session: Database,
    provider: Provider,
):
    require_scope(principal, "work:write")
    return service.recover(session, principal, work_id, body.obligation_key, provider)


@router.get("/work/{work_id}/followup/preparation-recovery")
def preparation_recovery_state(work_id: UUID, principal: Principal, session: Database):
    require_scope(principal, "work:read")
    return preparation_recovery.projection(session, principal, work_id)


@router.post("/work/{work_id}/followup/preparation-recovery")
def preparation_recovery_grant(work_id: UUID, body: preparation_recovery.PreparationRecoveryBody,
                               principal: Principal, session: Database, provider: Provider):
    require_scope(principal, "work:write")
    with session.begin():
        return preparation_recovery.grant(session, principal, work_id, body, provider)


@router.get("/followups/reviews")
def review_queue(
    principal: Reviewer,
    session: Database,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    from src.crm.services.followup_review import queue_page

    return queue_page(session, principal, cursor, limit)


@router.get("/followups/reviews/{request_id}")
def review_item(request_id: UUID, principal: Reviewer, session: Database):
    _, item = find_request(session, principal, request_id)
    return request_projection(item, datetime.now(UTC))


@router.post("/followups/reviews/{request_id}/verdict", status_code=201)
def review_verdict(
    request_id: UUID,
    body: VerdictBody,
    principal: Reviewer,
    session: Database,
    provider: Provider,
):
    with session.begin():
        return service.accept_verdict(session, principal, request_id, body, provider)


from src.crm.services.followup_recovery_credit import RecoveryCreditBody, grant as grant_recovery_credit


@router.post("/work/{work_id}/followup/recovery-credit")
def recovery_credit(work_id: UUID, body: RecoveryCreditBody, principal: Principal, session: Database, provider: Provider):
    require_scope(principal, "work:write")
    with session.begin():
        return grant_recovery_credit(session, principal, work_id, body, provider)
