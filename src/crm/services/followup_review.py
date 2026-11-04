"""Immutable FU payload review byte protocol; a parsed verdict is NOT authority."""

import json

from dashboard.app.schemas.followups import ModelVerdict


def strict_json(raw, *, maximum=1024 * 1024):
    if (
        not isinstance(raw, (str, bytes))
        or len(raw.encode("utf-8") if isinstance(raw, str) else raw) > maximum
    ):
        raise ValueError("invalid_json_size")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate_json_key")
            value[key] = item
        return value

    def invalid_constant(value):
        raise ValueError("invalid_json_constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def parse_verdict(raw):
    return ModelVerdict.model_validate(strict_json(raw, maximum=16 * 1024))


# Server-only ledger helpers. None of these accept caller-provided authority.
import hashlib
import os
import secrets
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select

from src.crm.persistence.models import AgentWork, AuditEvent
from src.crm.services.followup_context import canonical, digest


class FollowupError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def utc(value):
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ReviewPrincipal:
    workspace_id: UUID
    actor_id: UUID
    source_scopes: frozenset[str]
    config_hash: str
    scopes: frozenset[str] = frozenset({"followups:review"})


def review_configuration():
    try:
        from src.crm.connectors.note_drafts import valid_address

        if os.environ.get("CRM_FOLLOWUP_REVIEW_ENABLED") != "true":
            raise ValueError
        token = os.environ["CRM_FOLLOWUP_REVIEW_BEARER_TOKEN"]
        workspace = UUID(os.environ["CRM_FOLLOWUP_REVIEW_WORKSPACE_ID"])
        actor = UUID(os.environ["CRM_FOLLOWUP_REVIEW_ACTOR_ID"])
        scopes = frozenset(os.environ["CRM_FOLLOWUP_REVIEW_SOURCE_SCOPES"].split(","))
        if (
            len(token.encode()) < 32
            or not scopes
            or actor == uuid5(workspace, "head-of-sales")
        ):
            raise ValueError
        for scope in scopes:
            if not scope.startswith("mailbox:"):
                raise ValueError
            valid_address(scope.removeprefix("mailbox:"))
        # Configuration collisions cannot retrofit generic tokens into reviewers.
        for key, value in os.environ.items():
            if (
                key != "CRM_FOLLOWUP_REVIEW_BEARER_TOKEN"
                and key.startswith("CRM_")
                and "TOKEN" in key
                and value
            ) and secrets.compare_digest(token, value):
                raise ValueError
        config_hash = digest(
            {
                "workspace": str(workspace),
                "actor": str(actor),
                "sources": sorted(scopes),
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            }
        )
        return ReviewPrincipal(workspace, actor, scopes, config_hash), token
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise FollowupError("review_capability_disabled", 503) from None


def ledger_name(work):
    if "followup_anchor_v1" in work.payload:
        return "followup_anchor_v1"
    if "followup_v1" in work.payload:
        return "followup_v1"
    raise FollowupError("not_canonical_followup")


def save_ledger(work, ledger):
    work.payload = deepcopy(work.payload) | {ledger_name(work): deepcopy(ledger)}


def audit(session, work, actor, action, details):
    session.add(
        AuditEvent(
            workspace_id=work.workspace_id,
            command_id=uuid4(),
            actor_id=actor,
            action="followup." + action,
            entity_type="agent_work",
            entity_id=work.id,
            details=details,
        )
    )


def issue_request(session, work, package, lease_token, now):
    configured, _ = review_configuration()
    if (
        configured.workspace_id != work.workspace_id
        or "mailbox:" + package["mailbox"] not in configured.source_scopes
    ):
        raise FollowupError("review_assignment_missing", 503)
    state = deepcopy(work.payload[ledger_name(work)])
    review = state.setdefault("review", {"current_request_id": None, "requests": []})
    from src.crm.services.followup_preparation_recovery import review_evidence
    recovery_evidence = review_evidence(state)
    if recovery_evidence:
        package = package | {'preparation_recovery': recovery_evidence}
    semantic_hash = digest(package)
    allowance = state.get('preparation_recovery') or {}
    if allowance.get('technical_review_successor'):
        from src.crm.services.followup_technical_review import same_source
        if not same_source(work, package.get('context') or {}):
            raise FollowupError('technical_review_source_changed')
        successors = [r for r in review['requests']
                      if r['package']['request_id'] not in allowance['prior_request_ids']]
        if successors:
            old = successors[0]
            if (len(successors) == 1 and old['semantic_hash'] == semantic_hash
                    and old['lease_token'] == str(lease_token)
                    and old['config_hash'] == configured.config_hash
                    and old['status'] in ('pending_review', 'approved')
                    and timestamp(old['package']['expires_at']) > now):
                return old
            raise FollowupError('technical_review_successor_exhausted')
    if review["requests"]:
        old = review["requests"][-1]
        if old["semantic_hash"] == semantic_hash and old["status"] == "rejected":
            raise FollowupError("review_rejected")
        if (
            old["semantic_hash"] == semantic_hash
            and old["lease_token"] == str(lease_token)
            and old["config_hash"] == configured.config_hash
        ) and (
            old["status"] in ("pending_review", "approved")
            and timestamp(old["package"]["expires_at"]) > now
        ):
            return old
        if (old["status"] in ("pending_review", "approved")
                and old['package']['request_id'] not in (state.get('preparation_recovery') or {}).get('prior_request_ids', [])
                and old['package']['request_id'] not in (state.get('expired_handoff') or {}).get('protected_request_ids', [])):
            old["status"] = "invalidated"
    request_id = str(uuid4())
    package = package | {
        "protocol": "followup-review/v1.1",
        "request_id": request_id,
        "work_id": str(work.id),
        "workspace_id": str(work.workspace_id),
        "reviewer_actor_id": str(configured.actor_id),
        "nonce": secrets.token_hex(32),
        "created_at": utc(now),
        "expires_at": utc(min(now + timedelta(seconds=300), work.lease_until)),
    }
    if len(canonical(package)) > 1024 * 1024:
        raise FollowupError("review_package_too_large")
    item = {
        "package": package,
        "request_hash": digest(package),
        "status": "pending_review",
        "consumed_at": None,
        "receipt": None,
        "semantic_hash": semantic_hash,
        "lease_token": str(lease_token),
        "config_hash": configured.config_hash,
    }
    review["requests"].append(item)
    review["current_request_id"] = request_id
    state["decision"] = None
    save_ledger(work, state)
    audit(
        session,
        work,
        configured.actor_id,
        "review_requested",
        {"request_id": request_id, "request_hash": item["request_hash"]},
    )
    session.flush()
    return item


def find_request(session, principal, request_id, *, lock=False):
    # Requests remain in the one canonical ledger. No parallel business table.
    query = select(AgentWork).where(AgentWork.workspace_id == principal.workspace_id)
    for work in session.scalars(query):
        if not any(k in work.payload for k in ("followup_v1", "followup_anchor_v1")):
            continue
        for item in (
            work.payload[ledger_name(work)].get("review", {}).get("requests", [])
        ):
            package = item["package"]
            if package["request_id"] != str(request_id):
                continue
            if (
                package["reviewer_actor_id"] != str(principal.actor_id)
                or "mailbox:" + package["mailbox"] not in principal.source_scopes
            ):
                raise FollowupError("review_not_found", 404)
            if lock:
                session.refresh(work, with_for_update=True)
                item = next(
                    i
                    for i in work.payload[ledger_name(work)]["review"]["requests"]
                    if i["package"]["request_id"] == str(request_id)
                )
            return work, deepcopy(item)
    raise FollowupError("review_not_found", 404)


def initial_anchor(mailbox, lead_id, thread_id, message_id):
    """Untrusted SENT observation is only a request for complete fresh review."""
    return {
        "schema": "ordinary-followup-anchor/v1",
        "purpose": "anchor_classification",
        "mailbox": mailbox,
        "lead_id": str(lead_id),
        "thread_id": thread_id,
        "provider_message_id": message_id,
        "classification": None,
        "decision": None,
        "assessment": {
            "disposition": "needs_context",
            "reason_code": "anchor_unclassified",
            "not_before": None,
            "evidence_refs": [],
        },
        "review": {"current_request_id": None, "requests": []},
    }


def public_payload(payload):
    value = deepcopy(payload)
    for name in ("followup_v1", "followup_anchor_v1"):
        if name not in value:
            continue
        ledger = value[name]
        ledger.pop('expired_handoff', None)
        if ledger.get('preparation_recovery'):
            ledger['preparation_recovery'].pop('evidence_text', None)
        review = ledger.pop("review", {})
        current = next(
            (
                r
                for r in review.get("requests", [])
                if r["package"]["request_id"] == review.get("current_request_id")
            ),
            None,
        )
        if current:
            package = current["package"]
            ledger["review"] = {
                "request_id": package["request_id"],
                "status": request_projection(current, datetime.now(UTC))["status"],
                "reviewer_receipt_id": current["receipt"]["id"]
                if current["receipt"]
                else None,
                "context_hash": package["context_hash"],
                "payload_sha256": package["payload_sha256"],
                "expires_at": package["expires_at"],
                "reason_code": current["status"],
            }
        else:
            ledger["review"] = None
    return value


def queue_page(session, principal, cursor=None, limit=20, now=None):
    import base64
    import hmac

    now = now or datetime.now(UTC)
    binding = {
        "actor": str(principal.actor_id),
        "workspace": str(principal.workspace_id),
        "sources": sorted(principal.source_scopes),
    }
    last = None
    if cursor:
        try:
            raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
            data = strict_json(raw, maximum=4096)
            if set(data) != {"body", "mac"} or set(data["body"]) != {
                "binding",
                "after",
            }:
                raise ValueError
            mac = hmac.new(
                principal.config_hash.encode(), canonical(data["body"]), hashlib.sha256
            ).hexdigest()
            if (
                not hmac.compare_digest(data["mac"], mac)
                or data["body"]["binding"] != binding
            ):
                raise ValueError
            last = tuple(data["body"]["after"])
        except (ValueError, KeyError, TypeError):
            raise FollowupError("review_cursor_invalid") from None
    items = []
    for work in session.scalars(
        select(AgentWork).where(AgentWork.workspace_id == principal.workspace_id)
    ):
        if not any(k in work.payload for k in ("followup_v1", "followup_anchor_v1")):
            continue
        for item in (
            work.payload[ledger_name(work)].get("review", {}).get("requests", [])
        ):
            p = item["package"]
            key = (p["created_at"], p["request_id"])
            if (
                item["status"] == "pending_review"
                and timestamp(p["expires_at"]) > now
                and p["reviewer_actor_id"] == str(principal.actor_id)
                and "mailbox:" + p["mailbox"] in principal.source_scopes
                and item["config_hash"] == principal.config_hash
                and (last is None or key > last)
            ):
                items.append(
                    (
                        key,
                        {
                            "request_id": p["request_id"],
                            "work_id": p["work_id"],
                            "request_hash": item["request_hash"],
                            "expires_at": p["expires_at"],
                        },
                    )
                )
    items.sort(key=lambda v: v[0])
    page = items[:limit]
    next_cursor = None
    if len(items) > limit:
        body = {"binding": binding, "after": list(page[-1][0])}
        mac = hmac.new(
            principal.config_hash.encode(), canonical(body), hashlib.sha256
        ).hexdigest()
        next_cursor = base64.urlsafe_b64encode(
            canonical({"body": body, "mac": mac})
        ).decode()
    return {"items": [i[1] for i in page], "next_cursor": next_cursor}


def request_projection(item, now):
    status = item["status"]
    if (
        status in ("pending_review", "approved")
        and timestamp(item["package"]["expires_at"]) <= now
    ):
        status = "expired"
    return {
        "package": item["package"],
        "request_hash": item["request_hash"],
        "status": status,
        "reviewer_receipt_id": item["receipt"]["id"] if item["receipt"] else None,
    }
