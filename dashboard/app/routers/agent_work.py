"""Private automation work API; browser reads remain under CRM principal auth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from src.crm.connectors.gmail_cooldown import GmailRateLimited
import secrets
from typing import Annotated, Any, Literal
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, case
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from dashboard.app.db import create_database_engine
from dashboard.app.feature_flags import require_postgres_command_writer
from dashboard.app.security import CRMPrincipal, require_crm_principal
from dashboard.app.routers.agent_events import _consume_rate_limit
from src.crm.persistence.models import (
    AgentWork,
    Lead,
    Task,
    Activity,
    AuditEvent,
    Account,
    Contact,
)
from src.crm.services.agent_work_service import (
    claim_work,
    enqueue_task_work,
    lead_is_suppressed,
    fail_work,
    finish_work,
    resolve_waiting_review,
    locked_work,
    serialize_work,
    validate_lease,
    WorkConflict,
)
from src.crm.services.callback_execution import execute_callback
from src.crm.services.jev_recalibration import RecalibrationBody, recalibrate
from src.crm.services.note_fact_adjudication import WithdrawFactBody, withdraw_fact
from src.crm.services.historical_task_correction import HistoricalDueDateBody, correct_historical_due_date
from src.crm.services.jev_retry import RetryBody, RetryCommand
from src.crm.services.note_status_service import with_note_processing
from src.crm.services.commercial_profile import ProfileBody, RevertBody

router = APIRouter()


@dataclass(frozen=True)
class AutomationPrincipal:
    workspace_id: UUID
    actor_id: UUID
    scopes: frozenset[str]
    source_scopes: frozenset[str]


def require_automation_principal(request: Request) -> AutomationPrincipal:
    """Configured scoped service credential; never accepts browser cookies or tenant input."""
    try:
        token = os.environ["CRM_AUTOMATION_BEARER_TOKEN"]
        workspace_id = UUID(os.environ["CRM_AUTOMATION_WORKSPACE_ID"])
        scopes = frozenset(os.environ["CRM_AUTOMATION_SCOPES"].split(","))
        issued = datetime.fromisoformat(
            request.headers.get("x-agent-timestamp", "").replace("Z", "+00:00")
        )
        authorization = request.headers.get("authorization", "")
        if (
            len(token) < 32
            or not secrets.compare_digest(authorization, "Bearer " + token)
            or issued.tzinfo is None
            or abs(datetime.now(UTC) - issued) > timedelta(minutes=5)
        ):
            raise ValueError
        if request.headers.get("origin"):
            raise ValueError
    except (KeyError, ValueError, TypeError, UnicodeEncodeError):
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    # Owner-authorized API automation throughput; all other buckets stay at 60/min.
    if not _consume_rate_limit(workspace_id, "automation", max_requests=50000000):
        raise HTTPException(
            status_code=429, detail="Too many requests", headers={"Retry-After": "60"}
        )
    return AutomationPrincipal(
        workspace_id,
        uuid5(workspace_id, "head-of-sales"),
        scopes,
        frozenset(
            filter(None, os.environ.get("CRM_AUTOMATION_SOURCE_SCOPES", "").split(","))
        ),
    )


def require_scope(principal, scope):
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail="Forbidden")
    require_postgres_command_writer()


def get_work_session():
    engine = create_database_engine()
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


Principal = Annotated[AutomationPrincipal, Depends(require_automation_principal)]
Database = Annotated[Session, Depends(get_work_session)]
_INTERNAL_JEV_KINDS = ('jev_note_budget', 'jev_note_decision')


def _note_jev_projection(session, workspace_id, lead_id, activity_id):
    """Only fresh advisory hints; raw historical receipts stay internal."""
    lead = session.scalar(select(Lead).where(Lead.workspace_id == workspace_id,
                                            Lead.id == lead_id))
    source = session.scalar(select(Activity).where(Activity.workspace_id == workspace_id,
                                                   Activity.id == activity_id))
    if lead is None or source is None or not (
            source.lead_id == lead.id or (lead.account_id and source.account_id == lead.account_id)):
        raise HTTPException(status_code=404, detail='Not found')
    from src.crm.services.jev_note_worker import _projection
    from src.crm.services.note_source_service import source_digest
    hints, receipts = _projection(session, workspace_id, lead_id, activity_id)
    superseded = session.scalar(select(Activity.id).where(Activity.workspace_id == workspace_id,
        Activity.supersedes_activity_id == source.id).limit(1))
    return {'hints': hints, 'fact_authority': False, 'receipt_ids': receipts,
        'activity_id': str(source.id), 'source_digest': source_digest(source),
        'source_status': 'superseded' if superseded else 'current'}


@router.get('/api/v1/leads/{lead_id}/notes/{activity_id}/jev-hints')
def note_jev_hints(lead_id: UUID, activity_id: UUID,
                   principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
                   session: Database):
    """Authenticated tenant projection only; no receipt bodies or human writes."""
    if principal.subject == 'public-browser' or 'crm:read' not in principal.permissions:
        raise HTTPException(status_code=403, detail='Forbidden')
    return _note_jev_projection(session, principal.workspace_id, lead_id, activity_id)


@router.get('/api/v1/agent/leads/{lead_id}/notes/{activity_id}/jev-hints')
def agent_note_jev_hints(lead_id: UUID, activity_id: UUID,
                       principal: Principal, session: Database):
    """Sales uses its existing scoped service credential, never browser auth."""
    require_scope(principal, 'work:read')
    return _note_jev_projection(session, principal.workspace_id, lead_id, activity_id)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimBody(StrictBody):
    worker_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=20)
    lease_seconds: int = Field(default=300, ge=30, le=600)
    work_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=20)
    kinds: (
        list[
            Literal[
                "calendar_callback",
                "call_followup",
                "reply_review",
                "email_update",
                "proposal_review",
                "identity_review",
                "sent_review",
                "calendar_review",
                "followup_due",
            ]
        ]
        | None
    ) = Field(default=None, min_length=1, max_length=9)


class LeaseBody(StrictBody):
    lease_token: UUID


class WorkResult(StrictBody):
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    next_action: dict[str, Any] | None = None


class FinishBody(LeaseBody):
    result: WorkResult
    status: Literal["completed", "waiting"] = "completed"


class ReviewResolutionResult(StrictBody):
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[dict[str, Any]] = Field(min_length=1, max_length=20)


class ResolveReviewBody(StrictBody):
    resolution_id: UUID
    expected_result_hash: str = Field(pattern="^[0-9a-f]{64}$")
    result: ReviewResolutionResult


class ReconcileCompletedTaskBody(StrictBody):
    resolution_id: UUID
    expected_result_hash: str = Field(pattern="^[0-9a-f]{64}$")
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    evidence_activity_id: UUID
    expected_evidence_digest: str = Field(pattern="^[0-9a-f]{64}$")
    source_activity_id: UUID | None = None
    expected_source_digest: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)


class FailBody(LeaseBody):
    reason: str = Field(default="Execution failed", min_length=1, max_length=256)
    retryable: bool = True


class NextActionBody(LeaseBody):
    expected_lead_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    due_at: AwareDatetime
    task_type: Literal["email", "follow_up"] = "follow_up"
    supersede_agent_followups: bool = False


def _list(session, workspace, status, limit):
    query = select(AgentWork).where(
        AgentWork.workspace_id == workspace,
        AgentWork.kind.notin_(("bulk_observation", "unmatched_observation", *_INTERNAL_JEV_KINDS)),
    )
    if status:
        query = query.where(AgentWork.status == status)
    return [
        with_note_processing(session, workspace, serialize_work(item))
        for item in session.scalars(
            query.order_by(
                case(
                    {
                        "running": 0,
                        "waiting": 1,
                        "failed": 2,
                        "queued": 3,
                        "completed": 4,
                    },
                    value=AgentWork.status,
                    else_=5,
                ),
                AgentWork.updated_at.desc(),
            ).limit(limit)
        )
    ]


@router.get("/api/v1/agent/work")
def list_work(
    principal: Principal,
    session: Database,
    status: Literal["queued", "running", "waiting", "completed", "failed"]
    | None = None,
    limit: int = 20,
):
    require_scope(principal, "work:read")
    return {
        "items": _list(session, principal.workspace_id, status, max(1, min(limit, 100)))
    }


@router.get("/api/v1/agent/work/audit")
def audit_work(principal: Principal, session: Database, cursor: UUID | None = None,
               cutoff: AwareDatetime | None = None, limit: int = 50):
    """Enumerate every non-internal work kind within one creation cutoff."""
    require_scope(principal, "work:read")
    cutoff = cutoff or datetime.now(UTC)
    limit = max(1, min(limit, 50))
    query = select(AgentWork).where(
        AgentWork.workspace_id == principal.workspace_id,
        AgentWork.created_at <= cutoff,
        AgentWork.kind.notin_(_INTERNAL_JEV_KINDS),
    )
    if cursor:
        query = query.where(AgentWork.id > cursor)
    rows = list(session.scalars(query.order_by(AgentWork.id).limit(limit + 1)))
    return {
        "items": [serialize_work(row) | {"created_at": row.created_at.astimezone(UTC).isoformat()}
                  for row in rows[:limit]],
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
        "cutoff": cutoff.astimezone(UTC).isoformat(),
        "scope": "all_non_internal_work_kinds",
        "excluded_kinds": list(_INTERNAL_JEV_KINDS),
    }


@router.get("/api/v1/agent/work/{work_id}")
def read_work(work_id: UUID, principal: Principal, session: Database):
    require_scope(principal, "work:read")
    row = session.scalar(
        select(AgentWork).where(
            AgentWork.workspace_id == principal.workspace_id,
            AgentWork.id == work_id,
            AgentWork.kind.notin_(_INTERNAL_JEV_KINDS),
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Work not found")
    return _context(session, principal.workspace_id, serialize_work(row))


@router.get("/api/v1/agent-work")
def human_work(
    session: Database,
    principal: Annotated[CRMPrincipal, Depends(require_crm_principal)],
    limit: int = 50,
):
    return {
        "items": _list(session, principal.workspace_id, None, max(1, min(limit, 100)))
    }


def _context(session, workspace_id, item):
    if item["lead_id"]:
        lead = session.scalar(
            select(Lead).where(
                Lead.workspace_id == workspace_id, Lead.id == UUID(item["lead_id"])
            )
        )
        if lead:
            account = session.get(Account, lead.account_id) if lead.account_id else None
            contact = session.get(Contact, lead.contact_id) if lead.contact_id else None
            item["context"] = {
                "lead_version": lead.version,
                "suppressed": lead_is_suppressed(session, lead),
                "company": account.display_name if account else lead.company_name,
                "contact_name": contact.full_name if contact else lead.contact_name,
                "contact_email": str(contact.primary_email)
                if contact and contact.primary_email
                else lead.contact_email,
                "stage": lead.stage,
                "contact_id": str(contact.id) if contact else None,
                "contact_phone": contact.phone if contact else lead.contact_phone,
                "company_contacts": [{"id":str(c.id),"name":c.full_name,"email":str(c.primary_email) if c.primary_email else None,
                    "phone":c.phone,"title":c.title,"version":c.version,"is_primary":c.is_primary}
                    for c in session.scalars(select(Contact).where(Contact.workspace_id==workspace_id,
                        Contact.account_id==lead.account_id).order_by(Contact.id))] if lead.account_id else [],
                "existing_followup_work": [{'id':str(w.id),'kind':w.kind,'status':w.status,
                    'task_id':str(w.task_id) if w.task_id else None,'available_at':w.available_at.isoformat()}
                    for w in session.scalars(select(AgentWork).where(AgentWork.workspace_id==workspace_id,
                        AgentWork.lead_id==lead.id,AgentWork.kind.in_(['followup_due','sent_review']))
                        .order_by(AgentWork.updated_at.desc()).limit(30))],
                "open_tasks": [
                    {
                        "id": str(t.id),
                        "type": t.task_type,
                        "title": t.title,
                        "due_at": t.due_at.isoformat(),
                        "source_rule": t.source_rule,
                        "version": t.version,
                        "status": t.status,
                    }
                    for t in session.scalars(
                        select(Task)
                        .where(
                            Task.workspace_id == workspace_id,
                            Task.lead_id == lead.id,
                            Task.status == "open",
                        )
                        .limit(30)
                    )
                ],
            }
            from src.crm.services.contact_channels import operational_context
            item["context"].update(operational_context(session, workspace_id, lead.id))
            if item["kind"] == "call_followup":
                from src.crm.services.note_source_service import note_context
                item["context"]["note_source"] = note_context(
                    session, workspace_id, lead.id, item["payload"]
                )
                from src.crm.services.note_contacts import latest_contact_phase
                item["context"]["contact_phase"] = latest_contact_phase(
                    session, workspace_id, UUID(item["id"]), item["context"]["note_source"])
    if item['kind'] == 'email_update' and 'context' in item:
        from src.crm.services.email_update_service import email_context
        item['context']['email_message'] = email_context(item['payload'])
    return with_note_processing(session, workspace_id, item)


def get_preparation_provider_factory(request: Request):
    # Lazy: unrelated claims never acquire a Gmail capability.
    from dashboard.app.routers.followups import get_followup_provider
    return lambda: get_followup_provider(request)


@router.post("/api/v1/agent/work/claim")
def claim(body: ClaimBody, principal: Principal, session: Database,
          preparation_provider_factory: Annotated[object, Depends(get_preparation_provider_factory)]):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            items = claim_work(session, principal.workspace_id, actor_id=principal.actor_id,
                preparation_principal=principal, preparation_provider_factory=preparation_provider_factory,
                **body.model_dump())
            return {"items": [_context(session, principal.workspace_id, item) for item in items]}
    except WorkConflict:
        raise HTTPException(status_code=409, detail='Claim context conflict') from None


class OperationalAdmissionBody(StrictBody):
    policy: Literal['moveauto24-changed-cause-admission/v1']
    work_id: str
    actor_id: str
    worker_id: Literal['hermes-head-of-sales-mac-mini-event']
    source_artifact: Literal['59e0b378b5557d75f5e9255241f3228a06f359f1b7a6671257c1864f70134b40']
    action_key: Literal['reconcile_moveauto24_return_20261104_v1']
    work_digest: str = Field(pattern='^[0-9a-f]{64}$')
    context_digest: str = Field(pattern='^[0-9a-f]{64}$')
    audit_id: str
    prior_error: str | None
    prior_worker_id: str | None
    prior_last_lease_token: str | None


@router.get('/api/v1/agent/work/{work_id}/moveauto-admission')
def operational_admission_preview(work_id: UUID, principal: Principal, session: Database):
    from src.crm.services.moveauto_operational_admission import preview
    require_scope(principal, 'work:write')
    try:
        with session.begin():
            return preview(session, principal, locked_work(session, principal.workspace_id, work_id))
    except (WorkConflict, ValueError):
        raise HTTPException(status_code=409, detail='Operational admission prerequisite changed') from None


@router.post('/api/v1/agent/work/{work_id}/moveauto-admission')
def operational_admission(work_id: UUID, body: OperationalAdmissionBody, principal: Principal, session: Database):
    from src.crm.services.moveauto_operational_admission import admit
    require_scope(principal, 'work:write')
    try:
        with session.begin():
            return admit(session, principal, locked_work(session, principal.workspace_id, work_id), body.model_dump())
    except (WorkConflict, ValueError, IntegrityError):
        raise HTTPException(status_code=409, detail='Operational admission CAS conflict') from None


class RecoverUndeliveredClaimBody(StrictBody):
    worker_id: str = Field(min_length=1,max_length=128)
    expected_updated_at: AwareDatetime
    expected_work_digest: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    expired_followup_context_hash: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    expected_expired_review_request_id: UUID | None = None
    expected_context_read_failure: Literal['provider_read_failed'] | None = None
    context_failure_observed_at: AwareDatetime | None = None
    context_failure_evidence_sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    expected_prepare_read_failure: Literal['provider_read_failed'] | None = None
    prepare_failure_observed_at: AwareDatetime | None = None
    prepare_failure_evidence_sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    false_reject_certificate_sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    false_reject_context_hash: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')


@router.post('/api/v1/agent/work/{work_id}/recover-claim')
def recover_undelivered_claim(work_id: UUID, body: RecoverUndeliveredClaimBody,
                              principal: Principal, session: Database):
    """One continuation of a consumed preparation with no accepted result.

    Retains attempts, grant, evidence, old reviews and send policy. A new review
    and all current business checks remain mandatory in the normal executor.
    """
    from uuid import uuid4
    from src.crm.services.followup_preparation_recovery import mailbox_readiness, expired_classification_handoff, prepare_get_failure_handoff, PALOPINA_CONSUMED_AUDIT, certified_prepare_false_reject, validate_false_reject_fresh_context, AMERICOLD_FALSE_REJECT_CERTIFICATES, AMERICOLD_PEER_FALSE_REJECT_CERTIFICATES
    require_scope(principal,'work:write')
    try:
        with session.begin():
            row=locked_work(session,principal.workspace_id,work_id)
            if body.expected_work_digest is not None or body.expired_followup_context_hash is not None:
                from src.crm.services.followup_expired_handoff import recover
                reply = recover(session, principal, row, body)
                reply['items'] = [_context(session,principal.workspace_id,item) for item in reply['items']]
                return reply
            state=(row.payload or {}).get('followup_v1') or (row.payload or {}).get('followup_anchor_v1') or {}
            grant=state.get('preparation_recovery') or {}
            if grant.get('status')!='consumed' or not grant.get('command_id'):
                raise WorkConflict('No consumed preparation to continue')
            command=uuid5(UUID(grant['command_id']),'lease-handoff-recovery/v1')
            previous=session.get(AuditEvent,command)
            now=datetime.now(UTC)
            context_retry=body.expected_context_read_failure=='provider_read_failed'
            original_handoff=previous
            prepare_retry=body.expected_prepare_read_failure=='provider_read_failed'
            false_reject = body.false_reject_certificate_sha256 is not None
            false_certificate, false_certificate_sha256 = AMERICOLD_FALSE_REJECT_CERTIFICATES.get(str(row.id), (None, None))
            if false_reject or body.false_reject_context_hash is not None:
                if (not false_reject or not body.false_reject_context_hash or context_retry or prepare_retry
                        or body.expected_expired_review_request_id is not None
                        or body.context_failure_observed_at is not None or body.context_failure_evidence_sha256 is not None
                        or body.prepare_failure_observed_at is not None or body.prepare_failure_evidence_sha256 is not None
                        or false_certificate is None
                        or body.false_reject_certificate_sha256 != false_certificate_sha256):
                    raise WorkConflict('Mixed or uncertified false rejection evidence')
                if previous and (previous.details.get('false_reject_certificate_sha256') != body.false_reject_certificate_sha256
                        or previous.details.get('false_reject_context_hash') != body.false_reject_context_hash
                        or previous.details.get('original_updated_at') != body.expected_updated_at.isoformat()):
                    raise WorkConflict('False rejection replay evidence changed')
            elif previous and previous.details.get('false_reject_certificate_sha256'):
                raise WorkConflict('False rejection continuation already used')
            if prepare_retry:
                if (context_retry or body.context_failure_observed_at is not None
                        or body.context_failure_evidence_sha256 is not None
                        or body.expected_expired_review_request_id is not None
                        or body.prepare_failure_observed_at is None
                        or body.prepare_failure_evidence_sha256 is None):
                    raise WorkConflict('Mixed or incomplete prepare GET evidence')
                if previous and (previous.details.get('resume_reason')!='transient_prepare_get_failed'
                        or previous.details.get('prepare_read_recovery') != {
                            'method':'GET','phase':'prepare','http_status':502,'reason_code':'provider_read_failed',
                            'observed_at':body.prepare_failure_observed_at.isoformat(),
                            'evidence_sha256':body.prepare_failure_evidence_sha256,
                            'original_consumed_audit_id':PALOPINA_CONSUMED_AUDIT,'maximum_continuations':1}):
                    raise WorkConflict('Prepare GET recovery evidence changed')
            elif body.prepare_failure_observed_at is not None or body.prepare_failure_evidence_sha256 is not None:
                raise WorkConflict('Unexpected prepare GET evidence')
            if context_retry and previous and previous.details.get('resume_reason')=='transient_prepare_get_failed':
                raise WorkConflict('Prepare GET continuation already used')
            if context_retry:
                if (not previous or previous.action!='followup.undelivered_claim_resumed'
                        or previous.workspace_id!=principal.workspace_id or previous.entity_id!=row.id
                        or previous.actor_id!=principal.actor_id
                        or not body.context_failure_evidence_sha256
                        or body.context_failure_observed_at is None):
                    raise WorkConflict('Transient context read evidence unavailable')
                command=uuid5(command,'preparation-context-read-retry/v1')
                previous=session.get(AuditEvent,command)
            elif body.context_failure_observed_at is not None or body.context_failure_evidence_sha256 is not None:
                raise WorkConflict('Unexpected context read evidence')
            if previous:
                if (previous.workspace_id!=principal.workspace_id or previous.entity_id!=row.id
                        or previous.actor_id!=principal.actor_id):raise WorkConflict('Recovery ownership changed')
                active=row.status=='running' and row.worker_id==body.worker_id and row.lease_until is not None and row.lease_until>now
                return {'items':[_context(session,principal.workspace_id,serialize_work(row,include_lease=True))] if active else [],
                        'status':row.status,'replayed':True}
            review=state.get('review') or {};prior=set(grant.get('prior_request_ids') or [])
            if false_reject:
                original_false=session.get(AuditEvent,UUID(grant['audit_id']))
                consumed_false=session.get(AuditEvent,UUID(false_certificate['consumed_audit_id']))
                if not certified_prepare_false_reject(row,state,grant,body,principal,original_false,consumed_false):
                    raise WorkConflict('Original false rejection evidence changed')
            transient_prepare_read=False
            if prepare_retry:
                original_prepare=session.get(AuditEvent,UUID(grant['audit_id']))
                consumed_prepare=session.get(AuditEvent,UUID(PALOPINA_CONSUMED_AUDIT))
                transient_prepare_read=prepare_get_failure_handoff(row,state,grant,body,principal,original_prepare,consumed_prepare)
                if not transient_prepare_read:
                    raise WorkConflict('Not the original unprepared GET prepare failure')
            transient_context_read=(context_retry and row.kind=='followup_due'
                and row.error=='followup_handoff:execution_error:AdapterError'
                and grant.get('policy')=='followup-preparation-recovery/v1'
                and row.result_hash is None
                and original_handoff.created_at <= body.context_failure_observed_at <= row.updated_at
                and review.get('status') is None
                and not any(review.get(k) is not None for k in ('current_request_id','request_id','reviewer_receipt_id','receipt'))
                and not any(r['package']['request_id'] not in prior for r in review.get('requests',[])))
            if context_retry and not transient_context_read:
                raise WorkConflict('Not an unprepared transient context read failure')
            expired_reviews=expired_classification_handoff(row,state,grant,now,body.expected_expired_review_request_id)
            if body.expected_expired_review_request_id is not None and not expired_reviews:
                raise WorkConflict('Expired classification review cannot continue')
            untouched=(row.error=='Worker lease expired' and not any(
                r['package']['request_id'] not in prior for r in review.get('requests',[])))
            unprepared_classification=(row.kind=='sent_review'
                and row.error=='followup_handoff:review_or_execution_not_finalized'
                and grant.get('policy')=='followup-preparation-recovery/v1'
                and grant.get('prior_request_ids')==[]
                and review.get('status') is None
                and isinstance(review.get('requests',[]),list) and not review.get('requests')
                and not any(review.get(k) is not None for k in (
                    'current_request_id','request_id','reviewer_receipt_id','receipt'))
                and row.result_hash is None)
            if (row.kind not in ('followup_due','sent_review') or row.status!='failed'
                    or not (untouched or expired_reviews or unprepared_classification or transient_context_read or transient_prepare_read or false_reject) or row.worker_id!=body.worker_id
                    or row.lease_token is not None or row.result is not None
                    or (row.lease_until is not None and row.lease_until>now)
                    or row.updated_at!=body.expected_updated_at
                    or state.get('classification') is not None
                    or state.get('intent') is not None or state.get('decision') is not None):
                raise WorkConflict('Preparation already advanced or work changed')
            original=session.get(AuditEvent,UUID(grant['audit_id']))
            if not original or original.workspace_id!=principal.workspace_id or original.entity_id!=row.id:
                raise WorkConflict('Original recovery evidence unavailable')
            if unprepared_classification and (original.action!='followup.preparation_recovery.granted'
                    or original.command_id!=UUID(grant['command_id'])
                    or (original.details.get('receipt') or {}).get('work_id')!=str(row.id)):
                raise WorkConflict('Original classification preparation evidence changed')
            ready=mailbox_readiness(session,row,now)
            if ready['status'] not in ('ready','not_applicable'):
                return {'items':[],'status':'deferred','readiness':ready}
            if transient_prepare_read or false_reject:
                lead=session.scalar(select(Lead).where(Lead.workspace_id==principal.workspace_id,Lead.id==row.lead_id).with_for_update())
                task=session.scalar(select(Task).where(Task.workspace_id==principal.workspace_id,Task.id==row.task_id).with_for_update())
                if (lead is None or task is None or lead.version!=state['expected_lead_version']
                        or lead_is_suppressed(session,lead) or task.status!='open'
                        or task.version!=state['task_version']):
                    raise WorkConflict('Prepare recovery task or lead changed')
            if false_reject:
                if str(row.id) in AMERICOLD_PEER_FALSE_REJECT_CERTIFICATES and (
                        task.workspace_id != row.workspace_id or task.lead_id != row.lead_id
                        or task.task_type != 'email' or task.source_rule != 'ordinary-followup/v1'
                        or task.due_at != datetime.fromisoformat(false_certificate['task_due_at'])
                        or task.due_at > now or row.available_at > now):
                    raise WorkConflict('False reject preparation not due')
                validate_false_reject_fresh_context(session,principal,row,body)
            session.add(AuditEvent(id=command,workspace_id=principal.workspace_id,command_id=command,
                actor_id=principal.actor_id,action='followup.undelivered_claim_resumed',entity_type='agent_work',entity_id=row.id,
                details={'original_grant_command':grant['command_id'],'original_consumed_at':grant['consumed_at'],
                    'normal_attempts_preserved':row.attempts,'no_subsequent_review_or_send_intent':not bool(expired_reviews),
                    'resume_reason':('certified_prepare_false_reject' if false_reject else
                        'transient_prepare_get_failed' if transient_prepare_read else
                        'transient_context_read_failed' if transient_context_read else
                        'expired_review_without_receipt' if expired_reviews else
                        'unprepared_classification_handoff' if unprepared_classification else 'undelivered_claim'),
                    'expired_reviews_preserved':expired_reviews or [],'fresh_review_required':True,
                    'context_read_retry':({'original_handoff_audit_id':str(original_handoff.id),
                        'method':'GET','phase':'context','http_status':502,'reason_code':'provider_read_failed',
                        'observed_at':body.context_failure_observed_at.isoformat(),
                        'evidence_sha256':body.context_failure_evidence_sha256,'maximum_continuations':1}
                        if transient_context_read else None),
                    'prepare_read_recovery':({'method':'GET','phase':'prepare','http_status':502,
                        'reason_code':'provider_read_failed','observed_at':body.prepare_failure_observed_at.isoformat(),
                        'evidence_sha256':body.prepare_failure_evidence_sha256,
                        'original_consumed_audit_id':PALOPINA_CONSUMED_AUDIT,'maximum_continuations':1}
                        if transient_prepare_read else None),
                    'false_reject_certificate_sha256':body.false_reject_certificate_sha256 if false_reject else None,
                    'false_reject_context_hash':body.false_reject_context_hash if false_reject else None,
                    'original_updated_at':body.expected_updated_at.isoformat() if false_reject else None,
                    'new_send_authority':False,'worker_id':body.worker_id}))
            row.status='running';row.lease_token=uuid4();row.lease_until=now+timedelta(seconds=600);row.updated_at=now
            session.flush()
            return {'items':[_context(session,principal.workspace_id,serialize_work(row,include_lease=True))],
                    'status':'running','replayed':False}
    except GmailRateLimited as exc:
        raise HTTPException(status_code=429,detail=exc.reason_code,headers=exc.headers) from None
    except (WorkConflict,ValueError,IntegrityError):
        raise HTTPException(status_code=409,detail='Claim recovery context conflict') from None


@router.get("/api/v1/agent/work/leases/owned")
def owned_claims(worker_id: str, principal: Principal, session: Database):
    """Recover a bounded worker handoff even when a claim response lost all IDs."""
    from datetime import datetime, UTC
    require_scope(principal, 'work:write')
    rows=list(session.scalars(select(AgentWork).where(AgentWork.workspace_id==principal.workspace_id,
        AgentWork.worker_id==worker_id,AgentWork.status=='running',AgentWork.lease_until>datetime.now(UTC),
        AgentWork.kind!='calendar_callback').order_by(AgentWork.lease_until).limit(3)))
    return {'items':[_context(session,principal.workspace_id,serialize_work(row,include_lease=True)) for row in rows]}


@router.get("/api/v1/agent/work/{work_id}/lease")
def owned_lease(work_id: UUID, worker_id: str, principal: Principal, session: Database):
    """Recover the SAME live lease after an ambiguous claim acknowledgement.

    No attempt increment, renewal, state transition or provider operation.
    Scope and worker binding are identical to the original authenticated claim.
    """
    from datetime import datetime, UTC
    require_scope(principal, "work:write")
    row=session.scalar(select(AgentWork).where(AgentWork.workspace_id==principal.workspace_id,
        AgentWork.id==work_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Work not found")
    if (row.status!='running' or row.worker_id!=worker_id
            or row.lease_until is None or row.lease_until<=datetime.now(UTC)):
        return {"items": [], "status": row.status, "recovered": False}
    return {"items": [_context(session,principal.workspace_id,serialize_work(row,include_lease=True))],
            "status": "running", "recovered": True}


@router.post("/api/v1/agent/work/{work_id}/finish")
def finish(work_id: UUID, body: FinishBody, principal: Principal, session: Database):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            row = locked_work(session, principal.workspace_id, work_id)
            if row.kind == 'call_followup' and (row.payload or {}).get('activity_id') and body.status == 'completed':
                raise WorkConflict('Source-bound note completion requires canonical note-plan')
            return finish_work(
                session,
                principal.workspace_id,
                work_id,
                body.lease_token,
                result=body.result.model_dump(exclude_none=True),
                status=body.status,
            )
    except (WorkConflict, ValueError):
        raise HTTPException(status_code=409, detail="Work conflict") from None


@router.post("/api/v1/agent/work/{work_id}/resolve")
def resolve_review(
    work_id: UUID, body: ResolveReviewBody, principal: Principal, session: Database
):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            return resolve_waiting_review(
                session,
                principal.workspace_id,
                work_id,
                actor_id=principal.actor_id,
                resolution_id=body.resolution_id,
                expected_result_hash=body.expected_result_hash,
                result=body.result.model_dump(),
            )
    except (WorkConflict, ValueError, IntegrityError):
        raise HTTPException(
            status_code=409, detail="Review resolution conflict"
        ) from None


@router.post("/api/v1/agent/work/{work_id}/fail")
def fail(work_id: UUID, body: FailBody, principal: Principal, session: Database):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            return fail_work(
                session, principal.workspace_id, work_id, **body.model_dump()
            )
    except WorkConflict:
        raise HTTPException(status_code=409, detail="Work conflict") from None


@router.post("/api/v1/agent/work/{work_id}/execute")
def execute(work_id: UUID, body: LeaseBody, principal: Principal, session: Database):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            return execute_callback(
                session, principal.workspace_id, work_id, body.lease_token
            )
    except WorkConflict:
        raise HTTPException(status_code=409, detail="Work conflict") from None
    except Exception:
        raise HTTPException(
            status_code=502, detail="Provider action not confirmed"
        ) from None


@router.post("/api/v1/agent/work/{work_id}/next-action")
def next_action(
    work_id: UUID, body: NextActionBody, principal: Principal, session: Database
):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            row = locked_work(session, principal.workspace_id, work_id)
            if row.kind == 'call_followup' and (row.payload or {}).get('activity_id'):
                raise WorkConflict('Source-bound notes require canonical note-plan')
            task_id = uuid5(work_id, "next-action")
            result = {
                "summary": body.title,
                "evidence": row.payload.get("evidence", []),
                "next_action": {
                    "task_id": str(task_id),
                    "owner": "head-of-sales",
                    "due_at": body.due_at.isoformat(),
                },
            }
            if row.status == "completed" and row.last_lease_token == body.lease_token:
                return finish_work(
                    session,
                    principal.workspace_id,
                    work_id,
                    body.lease_token,
                    result=result,
                )
            validate_lease(row, body.lease_token)
            lead = session.scalar(
                select(Lead)
                .where(
                    Lead.workspace_id == principal.workspace_id, Lead.id == row.lead_id
                )
                .with_for_update()
            )
            if (
                not lead
                or lead_is_suppressed(session, lead)
                or lead.version != body.expected_lead_version
                or body.due_at <= datetime.now(UTC)
            ):
                raise WorkConflict("Lead changed or invalid date")
            if body.supersede_agent_followups:
                tasks = session.scalars(
                    select(Task)
                    .where(
                        Task.workspace_id == principal.workspace_id,
                        Task.lead_id == lead.id,
                        Task.status == "open",
                        Task.task_type.in_(("email", "follow_up")),
                        Task.source_rule == "head_of_sales",
                    )
                    .with_for_update()
                )
                for task in tasks:
                    task.status = "cancelled"
            task = Task(
                id=task_id,
                workspace_id=principal.workspace_id,
                lead_id=lead.id,
                account_id=lead.account_id,
                task_type=body.task_type,
                title=body.title,
                due_at=body.due_at,
                owner_user_id=principal.actor_id,
                status="open",
                source_rule="head_of_sales",
            )
            session.add(task)
            lead.updated_at = datetime.now(UTC)
            session.add(
                Activity(
                    id=uuid5(work_id, "next-action-activity"),
                    workspace_id=principal.workspace_id,
                    account_id=lead.account_id,
                    lead_id=lead.id,
                    activity_type="task",
                    title=body.title,
                    occurred_at=datetime.now(UTC),
                    source_system="agent",
                    actor_type="agent",
                    actor_id=principal.actor_id,
                )
            )
            session.add(
                AuditEvent(
                    id=uuid5(work_id, "next-action-audit"),
                    workspace_id=principal.workspace_id,
                    command_id=uuid5(work_id, "next-action"),
                    actor_id=principal.actor_id,
                    action="agent.next_action_scheduled",
                    entity_type="lead",
                    entity_id=lead.id,
                    details={
                        "task_id": str(task_id),
                        "work_id": str(work_id),
                        "supersede_agent_followups": body.supersede_agent_followups,
                    },
                )
            )
            session.flush()
            enqueue_task_work(session, task)
            return finish_work(
                session,
                principal.workspace_id,
                work_id,
                body.lease_token,
                result=result,
            )
    except (WorkConflict, ValueError):
        raise HTTPException(status_code=409, detail="Work conflict") from None


@router.post("/api/v1/agent/work/{work_id}/jev-recalibrate")
def recalibrate_jev_receipt(
    work_id: UUID, body: RecalibrationBody, principal: Principal, session: Database
):
    require_scope(principal, "work:write")
    try:
        with session.begin():
            return recalibrate(session, principal, work_id, body)
    except (WorkConflict, ValueError, TypeError, KeyError):
        raise HTTPException(status_code=409, detail="Work conflict") from None


@router.post('/api/v1/agent/tasks/{task_id}/correct-historical-due-date')
def correct_documented_historical_date(task_id: UUID, body: HistoricalDueDateBody,
                                      principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    try:
        with session.begin():
            return correct_historical_due_date(session, principal, task_id, body)
    except (WorkConflict, ValueError, TypeError, KeyError):
        raise HTTPException(status_code=409, detail='Work conflict') from None


@router.post('/api/v1/agent/work/{work_id}/note-facts/withdraw')
def withdraw_interpreted_fact(work_id: UUID, body: WithdrawFactBody,
                             principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    try:
        with session.begin():
            return withdraw_fact(session, principal, work_id, body)
    except (WorkConflict, ValueError, TypeError, KeyError):
        raise HTTPException(status_code=409, detail='Work conflict') from None


@router.post("/api/v1/agent/work/{work_id}/reconcile-completed-task")
def reconcile_completed_obligation(
    work_id: UUID, body: ReconcileCompletedTaskBody, principal: Principal, session: Database
):
    require_scope(principal, "work:write")
    from src.crm.services.stale_work_reconciliation import reconcile_completed_task
    try:
        with session.begin():
            return reconcile_completed_task(
                session, principal.workspace_id, principal.actor_id, work_id, body,
            )
    except (WorkConflict, ValueError, IntegrityError):
        raise HTTPException(status_code=409, detail="Work conflict") from None


class NoteFact(StrictBody):
    field: Literal['answer_kind','useful','decision_maker','interlocutor_role','repeat_reason']
    value: Any
    quote: str = Field(min_length=1, max_length=2000)


class DraftContent(StrictBody):
    recipient: str = Field(min_length=3, max_length=254)
    additional_to: list[str] = Field(default_factory=list, max_length=9)
    cc: list[str] = Field(default_factory=list, max_length=10)
    include_presentation: bool | None = None
    thread_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r'^[0-9a-fA-F]+$')
    subject: str = Field(min_length=1, max_length=240)
    body_html: str = Field(min_length=1, max_length=20000)


class NoteContact(StrictBody):
    contact_id: UUID | None = None
    expected_contact_version: int | None = Field(default=None, ge=1)
    full_name: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=64)
    make_primary: bool = False
    quote: str = Field(min_length=1, max_length=1000)


class NoteAction(StrictBody):
    key: str = Field(pattern='^[a-z0-9_-]{1,64}$')
    task_type: Literal['call','email','follow_up']
    title: str = Field(min_length=1, max_length=512)
    due_at: AwareDatetime
    quote: str = Field(min_length=1, max_length=2000)
    existing_task_id: UUID | None = None
    draft: DraftContent | None = None
    expected_task_version: int | None = Field(default=None, ge=1)


class NoteContactsBody(StrictBody):
    command_id: UUID
    expected_lead_version: int = Field(ge=1)
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    contacts: list[NoteContact] = Field(min_length=1, max_length=10)


@router.post('/api/v1/agent/work/{work_id}/note-contacts')
def note_contacts(work_id: UUID, body: NoteContactsBody, principal: Principal, session: Database):
    """Complete the contact step of a cited note without replaying its emails."""
    from uuid import uuid5
    import hashlib,json
    from src.crm.persistence.models import AuditEvent
    from src.crm.services.note_plan_service import _locked_company_lead,_substantive_newer_context
    from src.crm.services.note_source_service import note_context
    from src.crm.services.note_contacts import apply_contacts, expand_contact_receipt
    from src.crm.services.contact_channels import operational_context
    require_scope(principal,'work:write')
    request=body.model_dump(mode='json')
    for change in request['contacts']:
        if change.get('title') is None:change.pop('title',None)
    digest=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    try:
        with session.begin():
            command=uuid5(work_id,'contacts:'+str(body.command_id))
            previous=session.get(AuditEvent,command)
            if previous:
                if previous.workspace_id!=principal.workspace_id or previous.details.get('request_hash')!=digest:
                    raise WorkConflict('Contact command changed')
                return expand_contact_receipt(session, principal.workspace_id, previous.details['receipt'])
            row=locked_work(session,principal.workspace_id,work_id)
            if row.kind!='call_followup':raise WorkConflict('Source is not a saved note')
            lead,_=_locked_company_lead(session,principal.workspace_id,row.lead_id)
            source=note_context(session,principal.workspace_id,lead.id,row.payload)
            newer = _substantive_newer_context(source)
            # Adding only a cited secondary email does not replay the note's
            # commercial actions or replace a primary contact. Provider email
            # history alone must not prevent recording that literal contact
            # after José has already pressed Send. Human changes still block.
            email_enrichment_only = all(
                change.contact_id is None and not change.make_primary
                and change.email and change.full_name is None and change.title is None
                and change.phone is None for change in body.contacts)
            if email_enrichment_only and not (source.get('newer_context') or {}).get('has_more'):
                newer = [item for item in newer if not (
                    item.get('actor_type') == 'service' and item.get('source_system') == 'gmail'
                    and item.get('activity_type') in {'email_sent', 'email_received'})]
            if (lead.version!=body.expected_lead_version or source.get('status')!='current'
                    or source.get('source_digest')!=body.source_digest or newer):
                raise WorkConflict('Source or contact context changed')
            changes=apply_contacts(session,principal,lead,command,source,body.contacts)
            for i,change in enumerate(changes):
                session.add(AuditEvent(id=uuid5(command,str(i)),workspace_id=principal.workspace_id,
                    command_id=uuid5(command,str(i)),actor_id=principal.actor_id,
                    action='agent.contact_from_note',entity_type='contact',entity_id=UUID(change['contact_id']),details=change))
            session.flush()
            receipt={'command_id':str(body.command_id),'work_id':str(work_id),
                'contacts':[c['contact_id'] for c in changes],
                'change_audit_ids':[str(uuid5(command,str(i))) for i in range(len(changes))],
                'source_activity_id':source['activity_id'],
                'lead_version':lead.version,'source_digest':body.source_digest,'external_actions':'none',
                **operational_context(session,principal.workspace_id,lead.id)}
            session.add(AuditEvent(id=command,workspace_id=principal.workspace_id,command_id=command,
                actor_id=principal.actor_id,action='agent.note_contacts_completed',entity_type='agent_work',
                entity_id=work_id,details={'request_hash':digest,'receipt':receipt}))
            return expand_contact_receipt(session,principal.workspace_id,receipt)
    except (WorkConflict,ValueError,IntegrityError):
        raise HTTPException(status_code=409,detail='Contact evidence or version conflict') from None


class NoteDisposition(StrictBody):
    target_stage: Literal['new', 'contacted', 'qualified', 'meeting_booked', 'meeting_held',
                          'proposal_requested', 'proposal_sent', 'negotiation', 'won', 'lost', 'not_a_fit']
    quote: str = Field(min_length=1, max_length=500)


class NotePlanBody(LeaseBody):
    jev_receipt_id: UUID | None = None
    disposition: NoteDisposition | None = None
    expected_lead_version: int = Field(ge=1)
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    facts: list[NoteFact] = Field(default_factory=list, max_length=5)
    contacts: list[NoteContact] = Field(default_factory=list, max_length=10)
    actions: list[NoteAction] = Field(default_factory=list, max_length=5)
    summary: str = Field(min_length=1, max_length=2000)


@router.post('/api/v1/agent/work/{work_id}/note-plan')
def note_plan(work_id: UUID, body: NotePlanBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from src.crm.services.note_plan_service import apply_note_plan
    try:
        with session.begin():
            return apply_note_plan(session, principal, work_id, body)
    except GmailRateLimited as exc:
        # The failed plan transaction has rolled back. Provider unavailability
        # is not a bad reasoning attempt and must not exhaust this saved note.
        with session.begin():
            row = locked_work(session, principal.workspace_id, work_id)
            validate_lease(row, body.lease_token)
            if row.kind not in ('call_followup', 'email_update'):
                raise HTTPException(409, 'Work conflict') from None
            before_attempts = row.attempts
            row.attempts = max(0, row.attempts - 1)
            row.status = 'queued'
            row.available_at = datetime.fromtimestamp(exc.retry_at, UTC)
            row.error = 'provider_rate_limited'
            row.last_lease_token = row.lease_token
            row.lease_token = row.lease_until = None
            audit_id = uuid5(work_id, 'gmail-quota:' + str(body.lease_token))
            session.add(AuditEvent(id=audit_id, workspace_id=principal.workspace_id,
                command_id=audit_id, actor_id=principal.actor_id,
                action='agent.note_provider_deferred', entity_type='agent_work', entity_id=work_id,
                details={'reason':'provider_rate_limited','retry_after':exc.retry_after,
                         'attempts_before':before_attempts,'attempts_after':row.attempts}))
        raise HTTPException(429, exc.reason_code, headers=exc.headers) from None
    except (WorkConflict, ValueError, TypeError):
        raise HTTPException(status_code=409, detail='Work conflict') from None


@router.get('/api/v1/agent/leads/{lead_id}/notes/{activity_id}/facts')
def note_facts(lead_id: UUID, activity_id: UUID, principal: Principal, session: Database):
    require_scope(principal, 'work:read')
    from src.crm.services.jev_note_writer_bridge import read_facts
    try:
        return read_facts(session, principal.workspace_id, lead_id, activity_id)
    except LookupError:
        raise HTTPException(status_code=404, detail='Source unavailable') from None


@router.get('/api/v1/agent/leads/{lead_id}/commercial-profile')
def commercial_profile(lead_id: UUID, principal: Principal, session: Database):
    require_scope(principal, 'legacy:read')
    from src.crm.services.commercial_profile import read_profile
    from src.crm.services.command_service import CommandConflictError
    try:
        return read_profile(session, principal.workspace_id, lead_id)
    except CommandConflictError:
        raise HTTPException(status_code=404, detail='Profile unavailable') from None


def _write_commercial_profile(lead_id, principal, session, body):
    require_scope(principal, 'legacy:write')
    from dataclasses import asdict
    from sqlalchemy.orm import sessionmaker
    from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
    from src.crm.services.commercial_profile import CommercialProfileService
    from src.crm.services.command_service import HumanCommandPrincipal, CommandConflictError
    try:
        with SqlAlchemyUnitOfWork(sessionmaker(session.get_bind())) as uow:
            result = CommercialProfileService(uow).apply(HumanCommandPrincipal(
                principal.actor_id, principal.workspace_id, frozenset({'crm:lead:edit'})), lead_id, body)
            uow.commit()
            return asdict(result)
    except (CommandConflictError, IntegrityError):
        raise HTTPException(status_code=409, detail='Profile conflict') from None


@router.post('/api/v1/agent/leads/{lead_id}/commercial-profile')
def update_commercial_profile(lead_id: UUID, body: ProfileBody, principal: Principal, session: Database):
    return _write_commercial_profile(lead_id, principal, session, body)


@router.post('/api/v1/agent/leads/{lead_id}/commercial-profile/revert')
def revert_commercial_profile(lead_id: UUID, body: RevertBody, principal: Principal, session: Database):
    return _write_commercial_profile(lead_id, principal, session, body)


class JevFactsBody(StrictBody):
    jev_receipt_id: UUID
    expected_lead_version: int = Field(ge=1)
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')


@router.get('/api/v1/agent/notes/source-corpus')
def personal_note_corpus(principal: Principal, session: Database, cursor: UUID | None = None,
                        cutoff: AwareDatetime | None = None, limit: int = 100):
    require_scope(principal, 'work:read')
    from src.crm.services.jose_note_sources import source_corpus
    return source_corpus(session, principal.workspace_id, cutoff=cutoff or datetime.now(UTC),
        cursor=cursor, limit=max(1,min(limit,100)))


class PersonalSourceBody(StrictBody):
    source_digest: str = Field(pattern='^[0-9a-f]{64}$')
    expected_lead_version: int = Field(ge=1, strict=True)


@router.post('/api/v1/agent/leads/{lead_id}/notes/{activity_id}/personal-work')
def prepare_personal_note(lead_id: UUID, activity_id: UUID, body: PersonalSourceBody,
                          principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from src.crm.services.jose_note_sources import prepare_source
    try:
        with session.begin():
            return prepare_source(session, principal, lead_id, activity_id, body)
    except (WorkConflict, ValueError, TypeError, KeyError):
        raise HTTPException(status_code=409, detail='Source conflict') from None


@router.post('/api/v1/agent/work/{work_id}/jev-classify-personal')
async def classify_personal_note(work_id: UUID, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from sqlalchemy.orm import sessionmaker
    from src.crm.services.jev_note_runtime import classify_authorized_work
    return await classify_authorized_work(sessionmaker(session.get_bind()), principal.workspace_id,
        work_id, personal=True)


@router.post('/api/v1/agent/work/{work_id}/personal-facts')
def apply_personal_note(work_id: UUID, body: JevFactsBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from src.crm.services.jose_note_service import apply_classification
    try:
        with session.begin():
            return apply_classification(session, principal, work_id, body)
    except (WorkConflict, ValueError, TypeError, KeyError):
        raise HTTPException(status_code=409, detail='Source conflict') from None


@router.get('/api/v1/agent/leads/{lead_id}/notes/{activity_id}/personal-facts')
def read_personal_note(lead_id: UUID, activity_id: UUID, principal: Principal, session: Database):
    require_scope(principal, 'work:read')
    from src.crm.services.jose_note_service import read_classification
    try:
        return read_classification(session, principal.workspace_id, lead_id, activity_id)
    except LookupError:
        raise HTTPException(status_code=404, detail='Source unavailable') from None


@router.post('/api/v1/agent/work/{work_id}/jev-classify')
async def classify_jev_note(work_id: UUID, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from sqlalchemy.orm import sessionmaker
    from src.crm.services.jev_note_runtime import classify_authorized_work
    return await classify_authorized_work(sessionmaker(session.get_bind()), principal.workspace_id, work_id)


@router.post('/api/v1/agent/work/{work_id}/jev-retry')
async def retry_jev_note(work_id: UUID, body: RetryBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from sqlalchemy.orm import sessionmaker
    from src.crm.services.jev_note_runtime import classify_authorized_work
    result = await classify_authorized_work(sessionmaker(session.get_bind()), principal.workspace_id,
        work_id, retry=RetryCommand(principal.actor_id, body))
    if result.get('status') in ('fallback', 'disabled'):
        raise HTTPException(status_code=409, detail='Retry unavailable')
    return result


@router.post('/api/v1/agent/work/{work_id}/jev-classify-advisory')
async def classify_jev_note_advisory(work_id: UUID, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from sqlalchemy.orm import sessionmaker
    from src.crm.services.jev_note_runtime import classify_authorized_work
    return await classify_authorized_work(sessionmaker(session.get_bind()),
        principal.workspace_id, work_id, advisory=True)


@router.get('/api/v1/agent/work/{work_id}/jev-receipts/{receipt_id}')
def read_jev_receipt(work_id: UUID, receipt_id: UUID, principal: Principal, session: Database):
    require_scope(principal, 'work:read')
    from src.crm.services.jev_diagnostics import read_diagnostic
    try:
        return read_diagnostic(session, principal.workspace_id, work_id, receipt_id)
    except (LookupError, ValueError):
        raise HTTPException(status_code=404, detail='Receipt unavailable') from None


@router.post('/api/v1/agent/work/{work_id}/note-facts')
def apply_jev_note_facts(work_id: UUID, body: JevFactsBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from src.crm.services.note_plan_service import apply_note_plan
    try:
        with session.begin():
            return apply_note_plan(session, principal, work_id, body, projection_only=True)
    except (WorkConflict, ValueError, TypeError, KeyError):
        raise HTTPException(status_code=409, detail='Work conflict') from None


@router.get('/api/v1/agent/notes/audit')
def audit_notes(principal: Principal, session: Database, cursor: UUID | None = None,
                cutoff: AwareDatetime | None = None, limit: int = 20, verify_drafts: bool = True,
                created_after: AwareDatetime | None = None):
    require_scope(principal, 'work:read')
    cutoff=cutoff or datetime.now(UTC)
    limit=max(1,min(limit,50))
    query=select(AgentWork).where(AgentWork.workspace_id==principal.workspace_id,
        AgentWork.kind=='call_followup',AgentWork.created_at<=cutoff)
    if created_after:query=query.where(AgentWork.created_at>created_after)
    if cursor:query=query.where(AgentWork.id>cursor)
    rows=list(session.scalars(query.order_by(AgentWork.id).limit(limit+1)))
    return {'items':[with_note_processing(session,principal.workspace_id,
        serialize_work(row) | {'created_at': row.created_at.astimezone(UTC).isoformat()},
        verify_drafts=verify_drafts) for row in rows[:limit]],
        'next_cursor':str(rows[limit-1].id) if len(rows)>limit else None,
        'cutoff':cutoff.astimezone(UTC).isoformat(),
        'scope':'CRM obligations, recorded Calendar receipts, fresh read-only draft MIME; never proof of sending'}


class ReconcileNotesBody(StrictBody):
    limit: int = Field(default=50, ge=1, le=100)


@router.post('/api/v1/agent/notes/reconcile')
def reconcile_notes(body: ReconcileNotesBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    from src.crm.services.note_source_service import reconcile_note_sources
    with session.begin():
        return reconcile_note_sources(session, principal.workspace_id, body.limit)


class ObservationsBody(StrictBody):
    observations: list[dict[str, Any]] = Field(max_length=20)


@router.post("/api/v1/agent/providers/observations")
def observations(body: ObservationsBody, principal: Principal, session: Database):
    require_scope(principal, "providers:sync")
    import json
    from src.crm.services.google_observation_service import (
        apply_observation,
        ObservationError,
    )

    if len(json.dumps(body.model_dump()).encode()) > 256000:
        raise HTTPException(status_code=422, detail="Observation batch too large")
    if any(
        item.get("source_scope") not in principal.source_scopes
        for item in body.observations
    ):
        raise HTTPException(status_code=403, detail="Provider source not authorized")
    try:
        with session.begin():
            items = [
                apply_observation(session, principal.workspace_id, observation)
                for observation in body.observations
            ]
        return {
            "accepted": sum(not i["duplicate"] for i in items),
            "duplicates": sum(i["duplicate"] for i in items),
            "items": items,
        }
    except (ObservationError, ValueError, KeyError):
        raise HTTPException(
            status_code=422, detail="Invalid provider observation"
        ) from None


class GoogleSyncBody(StrictBody):
    limit: int = Field(default=20, ge=1, le=20)
    cursor: str | None = Field(default=None, max_length=2000)
    since: AwareDatetime | None = None


@router.post("/api/v1/agent/providers/google/sync")
def google_sync(body: GoogleSyncBody, principal: Principal, session: Database):
    require_scope(principal, "providers:sync")
    from src.crm.connectors.google_transport import GmailTransport

    mailbox = os.environ.get("CRM_GOOGLE_MAILBOX", "")
    credentials = os.environ.get("GOOGLE_GMAIL_CREDENTIALS_FILE", "")
    if not mailbox or not credentials:
        return {
            "configured": False,
            "accepted": 0,
            "duplicates": 0,
            "items": [],
            "reason": "Use local Google collector",
        }
    try:
        page = GmailTransport(
            credentials_file=credentials,
            mailbox_email=mailbox,
            since=body.since,
            limit=body.limit,
        ).fetch("mailbox:" + mailbox, body.cursor)
    except Exception:
        raise HTTPException(status_code=502, detail="Google read failed") from None
    result = observations(
        ObservationsBody(observations=page["observations"]), principal, session
    )
    return result | {
        "configured": True,
        "next_cursor": page["next_cursor"],
        "scanned_since": page["scanned_since"],
    }


from src.crm.services.email_update_service import EmailUpdateBody, apply_email_update

@router.post('/api/v1/agent/work/{work_id}/email-update')
def email_update(work_id: UUID, body: EmailUpdateBody, principal: Principal, session: Database):
    require_scope(principal, 'work:write')
    try:
        with session.begin():
            return apply_email_update(session, principal, work_id, body)
    except (WorkConflict, ValueError, IntegrityError):
        raise HTTPException(status_code=409, detail='Email or lead changed; refresh context') from None
