"""Source-addressed commercial phone facts, independent of legacy call metrics."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from src.crm.domain.commercial_kpi_contract import (
    RULE,
    AssessmentV1,
    CorrectionV1,
    ProposalV1,
    bounded_audit_details,
)
from src.crm.persistence.models import (
    Account,
    Activity,
    AuditEvent,
    Lead,
    SourceIdentity,
)
from src.crm.services.note_source_service import source_digest, source_snapshot

_CURSOR_KEY = secrets.token_bytes(32)


def encode_cursor(value):
    wire = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return (
        base64.urlsafe_b64encode(wire).decode().rstrip("=")
        + "."
        + hmac.new(_CURSOR_KEY, wire, hashlib.sha256).hexdigest()
    )


def decode_cursor(token):
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        encoded, signature = token.split(".")
        wire = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        if not hmac.compare_digest(
            signature, hmac.new(_CURSOR_KEY, wire, hashlib.sha256).hexdigest()
        ):
            raise ValueError
        value = json.loads(wire)
        if type(value) is not dict:
            raise ValueError
        return value
    except (ValueError, UnicodeError, TypeError):
        raise ValueError("Invalid or expired cursor; restart read") from None


def semantic_hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def cyclic_links(links):
    conflicts = set()
    for start in links:
        seen, node = set(), start
        while node in links:
            if node in seen:
                conflicts.update(seen)
                break
            seen.add(node)
            node = links[node]
    return conflicts


class SourceIndex:
    """A read-only workspace snapshot; history never depends on KPI audit writes."""

    def __init__(self, session, workspace_id):
        self.workspace_id = workspace_id
        self.failed_inputs = {}
        for audit in session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "workspace",
                AuditEvent.action == "commercial_kpi.reconcile_page",
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
        ):
            if audit.details.get("operation") == "failed_inputs":
                for fingerprint in audit.details["input_hashes"]:
                    self.failed_inputs[fingerprint] = {
                        key: audit.details[key]
                        for key in ("run_id", "failed_at", "code")
                    }
        self.activities = {
            row.id: row
            for row in session.scalars(
                select(Activity)
                .where(Activity.workspace_id == workspace_id)
                .execution_options(populate_existing=True)
            )
        }
        self.leads = {
            row.id: row
            for row in session.scalars(
                select(Lead)
                .where(Lead.workspace_id == workspace_id)
                .execution_options(populate_existing=True)
            )
        }
        self.companies = {}
        self.accounts = {
            row.id: row
            for row in session.scalars(
                select(Account)
                .where(Account.workspace_id == workspace_id)
                .execution_options(populate_existing=True)
            )
        }
        self.phone_history = defaultdict(list)
        self.identities = {
            row.id: {
                "id": str(row.id),
                "source_system": row.source_system,
                "source_scope": row.source_scope,
                "entity_kind": row.entity_kind,
                "external_id": row.external_id,
                "canonical_entity_type": row.canonical_entity_type,
                "canonical_entity_id": str(row.canonical_entity_id)
                if row.canonical_entity_id
                else None,
                "metadata": row.metadata_json,
            }
            for row in session.scalars(
                select(SourceIdentity)
                .where(SourceIdentity.workspace_id == workspace_id)
                .execution_options(populate_existing=True)
            )
        }
        self.duplicates = set()
        event_links = {}
        for row in self.activities.values():
            identity = self.identities.get(row.source_identity_id)
            if identity and identity["canonical_entity_type"] == "activity":
                target = UUID(identity["canonical_entity_id"])
                if target in self.activities and target != row.id:
                    self.duplicates.add(row.id)
                    event_links[row.id] = target
        self.conflicts = cyclic_links(event_links)
        self.duplicates.difference_update(self.conflicts)
        self.superseded = {
            row.supersedes_activity_id
            for row in self.activities.values()
            if row.supersedes_activity_id in self.activities
        }
        self.exclusions = {
            row.id: "superseded"
            if row.id in self.superseded
            else "duplicate"
            if row.id in self.duplicates
            else row.outcome_code
            for row in self.activities.values()
            if row.id in self.superseded
            or row.id in self.duplicates
            or row.outcome_code
            in {"non_phone", "test_scaffold", "voided", "cancelled_before_occurrence"}
        }
        for row in self.activities.values():
            lead = self.leads.get(row.lead_id)
            account = (lead.account_id if lead else None) or row.account_id
            seen = set()
            while (
                account in self.accounts
                and self.accounts[account].merged_into_account_id
            ):
                if account in seen:
                    raise ValueError("Canonical identity cycle")
                seen.add(account)
                account = self.accounts[account].merged_into_account_id
            company = f"account:{account}" if account else f"lead:{row.lead_id}"
            self.companies[row.id] = company
            if row.activity_type == "call":
                self.phone_history[company].append(row)
        for rows in self.phone_history.values():
            rows.sort(key=lambda row: (row.occurred_at, row.id))

    def context(self, activity_id):
        row = self.activities[UUID(str(activity_id))]
        company = self.companies[row.id]
        company_kind, company_id = company.split(":", 1)
        account = (
            self.accounts.get(UUID(company_id)) if company_kind == "account" else None
        )
        lead = self.leads.get(row.lead_id)
        company_name = (
            account.display_name if account else (lead.company_name if lead else None)
        )
        history = self.phone_history[company]
        uncertain_time = {"import", "migration", "system"}
        previous = [
            item
            for item in history
            if item.occurred_at < row.occurred_at
            and item.id not in self.exclusions
            and item.id not in self.conflicts
            and item.actor_type not in uncertain_time
            and row.actor_type not in uncertain_time
        ]
        ambiguous = row.actor_type in uncertain_time or any(
            item.id != row.id
            and item.id not in self.exclusions
            and item.occurred_at == row.occurred_at
            for item in history
        )
        coverage = {
            "state": "prior_attempt_proved"
            if previous
            else "ambiguous_order"
            if ambiguous
            else "incomplete",
            "oldest_known_at": history[0].occurred_at.isoformat() if history else None,
            "proof_activity_id": str(previous[-1].id) if previous else None,
        }
        manifest = [
            dict(
                source_snapshot(item),
                source_version=source_digest(item),
                actor_type=item.actor_type,
                source_identity=self.identities.get(item.source_identity_id),
            )
            for item in history
        ]
        related = [
            dict(source_snapshot(item), source_version=source_digest(item))
            for item in sorted(
                self.activities.values(), key=lambda item: (item.occurred_at, item.id)
            )
            if self.companies[item.id] == company
            and item.activity_type
            in {"note", "email_sent", "email_received", "meeting"}
        ]
        digest = source_digest(row)
        exclusion = self.exclusions.get(row.id)
        conflict = row.id in self.conflicts
        if conflict:
            coverage = {
                "state": "ambiguous_order",
                "oldest_known_at": coverage["oldest_known_at"],
                "proof_activity_id": None,
            }
        return dict(
            source_snapshot(row),
            canonical_company_id=company,
            company_display_name=company_name,
            source_digest=digest,
            source_version=digest,
            context_digest=semantic_hash(
                {
                    "source": digest,
                    "company": company,
                    "history": manifest,
                    "related_context": related,
                    "exclusion": exclusion,
                    "source_conflict": conflict,
                }
            ),
            source_conflict=conflict,
            eligibility="excluded" if exclusion else "eligible",
            exclusion_reason=exclusion,
            phone_attempt_kind="follow_up" if previous and not conflict else "unknown",
            history_coverage=coverage,
            history=manifest,
            related_context=related,
        )


class KPIConflict(ValueError):
    """A source, chain, or idempotency precondition no longer holds."""


def validate_references(index, context, value):
    allowed = {
        item["activity_id"] for item in context["history"] + context["related_context"]
    } | {context["activity_id"]}
    for ref in value["evidence_refs"]:
        source = index.activities.get(UUID(ref["activity_id"]))
        if (
            source is None
            or ref["activity_id"] not in allowed
            or source_digest(source) != ref["source_version"]
        ):
            raise ValueError("Evidence does not resolve to current context")
        if ref["field"] == "summary" and (
            not source.summary or ref["end"] > len(source.summary)
        ):
            raise ValueError("Evidence span exceeds source")
        if ref["field"] != "summary":
            field = (
                "source_identity_id"
                if ref["field"] == "source_identity"
                else ref["field"]
            )
            if getattr(source, field) is None:
                raise ValueError("Evidence field is absent")
    source = index.activities[UUID(context["activity_id"])]
    if value["phone_attempt_kind"] == "follow_up":
        proof = index.activities.get(
            UUID(value["history_coverage"]["proof_activity_id"])
        )
        if (
            proof is None
            or proof.activity_type != "call"
            or proof.id in index.exclusions
            or index.companies[proof.id] != context["canonical_company_id"]
            or proof.occurred_at >= source.occurred_at
            or proof.actor_type in {"import", "migration", "system"}
            or source.actor_type in {"import", "migration", "system"}
        ):
            raise KPIConflict(
                "Follow-up requires a distinct proved earlier phone attempt"
            )
    if value["phone_attempt_kind"] == "new":
        if context["history_coverage"]["state"] in {
            "prior_attempt_proved",
            "ambiguous_order",
        }:
            raise KPIConflict("New conflicts with observed phone chronology")
        if value["provenance"] == "inferred":
            proof = value["history_coverage"]["proof_activity_id"]
            if proof != context["activity_id"] or not any(
                ref["activity_id"] == proof and ref["field"] == "summary"
                for ref in value["evidence_refs"]
            ):
                raise ValueError(
                    "Inferred completeness requires affirmative source proof"
                )
    answer = (source.call_details or {}).get("answer_kind")
    if value["relevant"] == "yes" and (
        source.outcome_code in {"no_answer", "voicemail", "wrong_number"}
        or answer in {"no_answer", "voicemail", "wrong_number", "ivr"}
    ):
        raise ValueError("Relevance conflicts with explicit source answer")


def chain_head(rows):
    """Every predecessor must exist exactly once; DB ordering is not authority."""
    revisions = [row.details.get("revision") for row in rows]
    if any(type(revision) is not int or revision < 1 for revision in revisions):
        raise KPIConflict("Invalid audit chain")
    ordered = sorted(rows, key=lambda row: row.details["revision"])
    previous = None
    for revision, row in enumerate(ordered, 1):
        if (
            row.details["revision"] != revision
            or row.details.get("supersedes_audit_id") != previous
        ):
            raise KPIConflict("Conflicting audit chain")
        previous = str(row.id)
    return ordered[-1] if ordered else None


def assess(
    session,
    workspace_id,
    actor_id,
    *,
    command_id,
    expected_source_digest,
    expected_context_digest,
    proposal,
):
    """Append one versioned assessment; never commits the caller transaction."""
    proposal = ProposalV1.model_validate_json(json.dumps(proposal)).model_dump(
        mode="json"
    )
    activity_id = UUID(proposal["activity_id"])
    session.execute(
        select(Activity)
        .where(Activity.workspace_id == workspace_id, Activity.id == activity_id)
        .with_for_update()
    ).scalar_one()
    semantic = semantic_hash(
        {
            "proposal": proposal,
            "source": expected_source_digest,
            "context": expected_context_digest,
        }
    )
    old = session.scalar(
        select(AuditEvent).where(
            AuditEvent.workspace_id == workspace_id, AuditEvent.command_id == command_id
        )
    )
    if old is not None:
        if (
            old.action != "commercial_kpi.assessed"
            or old.entity_id != activity_id
            or old.details.get("semantic_hash") != semantic
        ):
            raise KPIConflict("Idempotency key conflict")
        return {
            "command_id": str(command_id),
            "audit_id": str(old.id),
            "revision": old.details["revision"],
            "replayed": True,
            "assessment": old.details["assessment"],
        }
    index = SourceIndex(session, workspace_id)
    context = index.context(activity_id)
    if (
        context["source_digest"] != expected_source_digest
        or context["context_digest"] != expected_context_digest
    ):
        raise KPIConflict("Source conflict")
    payload = dict(
        proposal,
        canonical_company_id=context["canonical_company_id"],
        source_digest=context["source_digest"],
        source_version=context["source_version"],
        context_digest=context["context_digest"],
        provenance="inferred",
        freshness="current",
        assessed_at=datetime.now(UTC).isoformat(),
    )
    value = AssessmentV1.model_validate_json(json.dumps(payload)).model_dump(
        mode="json"
    )
    validate_references(index, context, value)
    audits = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "activity",
                AuditEvent.entity_id == activity_id,
                AuditEvent.action == "commercial_kpi.assessed",
            )
        )
    )
    prior = chain_head(audits)
    revision = prior.details["revision"] + 1 if prior else 1
    audit = AuditEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        command_id=command_id,
        actor_id=actor_id,
        action="commercial_kpi.assessed",
        entity_type="activity",
        entity_id=activity_id,
        details={
            "rule_version": RULE,
            "revision": revision,
            "supersedes_audit_id": str(prior.id) if prior else None,
            "semantic_hash": semantic,
            "assessment": value,
        },
    )
    audit.details = bounded_audit_details(audit.details)
    session.add(audit)
    session.flush()
    return {
        "command_id": str(command_id),
        "audit_id": str(audit.id),
        "revision": revision,
        "replayed": False,
        "assessment": value,
    }


def correct(
    session,
    workspace_id,
    actor_id,
    activity_id,
    *,
    command_id,
    expected_source_digest,
    expected_context_digest,
    expected_override_revision,
    operation,
    correction,
):
    """Append to the independent human register; removal never deletes history."""
    if operation not in {"set", "remove"} or (
        (operation == "set") != (correction is not None)
    ):
        raise ValueError("Correction operation and payload disagree")
    if type(expected_override_revision) is not int or expected_override_revision < 0:
        raise ValueError("Invalid override revision")
    if correction is not None:
        correction = CorrectionV1.model_validate_json(
            json.dumps(correction)
        ).model_dump(mode="json")
    activity_id = UUID(str(activity_id))
    session.execute(
        select(Activity)
        .where(Activity.workspace_id == workspace_id, Activity.id == activity_id)
        .with_for_update()
    ).scalar_one()
    semantic = semantic_hash(
        {
            "activity_id": str(activity_id),
            "operation": operation,
            "correction": correction,
            "source": expected_source_digest,
            "context": expected_context_digest,
            "expected_override_revision": expected_override_revision,
        }
    )
    action = (
        "commercial_kpi.overridden"
        if operation == "set"
        else "commercial_kpi.override_removed"
    )
    old = session.scalar(
        select(AuditEvent).where(
            AuditEvent.workspace_id == workspace_id, AuditEvent.command_id == command_id
        )
    )
    if old is not None:
        if (
            old.action != action
            or old.entity_id != activity_id
            or old.details.get("semantic_hash") != semantic
        ):
            raise KPIConflict("Idempotency key conflict")
        return {
            "command_id": str(command_id),
            "audit_id": str(old.id),
            "override_revision": old.details["revision"],
            "replayed": True,
            "assessment": effective(session, workspace_id, activity_id)["assessment"],
        }
    index = SourceIndex(session, workspace_id)
    context = index.context(activity_id)
    if (
        context["source_digest"] != expected_source_digest
        or context["context_digest"] != expected_context_digest
    ):
        raise KPIConflict("Source conflict")
    human_rows = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "activity",
                AuditEvent.entity_id == activity_id,
                AuditEvent.action.in_(
                    ("commercial_kpi.overridden", "commercial_kpi.override_removed")
                ),
            )
        )
    )
    prior = chain_head(human_rows)
    revision = prior.details["revision"] + 1 if prior else 1
    if expected_override_revision != revision - 1:
        raise KPIConflict("Override revision conflict")
    if correction is not None:
        value = dict(
            correction,
            rule_version=RULE,
            activity_id=str(activity_id),
            canonical_company_id=context["canonical_company_id"],
            source_digest=expected_source_digest,
            source_version=expected_source_digest,
            context_digest=expected_context_digest,
            provenance="human",
            assessed_at=datetime.now(UTC).isoformat(),
            freshness="current",
        )
        AssessmentV1.model_validate_json(json.dumps(value))
        validate_references(index, context, value)
    details = {
        "rule_version": RULE,
        "revision": revision,
        "supersedes_audit_id": str(prior.id) if prior else None,
        "expected_source_digest": expected_source_digest,
        "expected_context_digest": expected_context_digest,
        "correction": correction,
        "semantic_hash": semantic,
    }
    audit = AuditEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        command_id=command_id,
        actor_id=actor_id,
        action="commercial_kpi.overridden"
        if operation == "set"
        else "commercial_kpi.override_removed",
        entity_type="activity",
        entity_id=activity_id,
        details=details,
    )
    audit.details = bounded_audit_details(audit.details)
    session.add(audit)
    session.flush()
    return {
        "command_id": str(command_id),
        "audit_id": str(audit.id),
        "override_revision": revision,
        "replayed": False,
        "assessment": effective(session, workspace_id, activity_id)["assessment"],
    }


def effective(session, workspace_id, activity_id, *, index=None, inference_only=False):
    index = index or SourceIndex(session, workspace_id)
    context = index.context(activity_id)
    rows = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "activity",
                AuditEvent.entity_id == UUID(str(activity_id)),
                AuditEvent.action == "commercial_kpi.assessed",
            )
        )
    )
    try:
        human_rows = (
            []
            if inference_only
            else list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.workspace_id == workspace_id,
                        AuditEvent.entity_type == "activity",
                        AuditEvent.entity_id == UUID(str(activity_id)),
                        AuditEvent.action.in_(
                            (
                                "commercial_kpi.overridden",
                                "commercial_kpi.override_removed",
                            )
                        ),
                    )
                )
            )
        )
        human = chain_head(human_rows)
        override_revision = human.details["revision"] if human else 0
        ancestor = index.activities[UUID(str(activity_id))]
        seen = {ancestor.id}
        while (
            not inference_only
            and human is None
            and ancestor.supersedes_activity_id in index.activities
        ):
            ancestor = index.activities[ancestor.supersedes_activity_id]
            if (
                ancestor.id in seen
                or index.companies[ancestor.id] != context["canonical_company_id"]
            ):
                raise KPIConflict("Conflicting source lineage")
            seen.add(ancestor.id)
            human = chain_head(
                list(
                    session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.workspace_id == workspace_id,
                            AuditEvent.entity_type == "activity",
                            AuditEvent.entity_id == ancestor.id,
                            AuditEvent.action.in_(
                                (
                                    "commercial_kpi.overridden",
                                    "commercial_kpi.override_removed",
                                )
                            ),
                        )
                    )
                )
            )
        if human is not None and human.action == "commercial_kpi.overridden":
            value = dict(
                human.details["correction"],
                rule_version=RULE,
                activity_id=str(activity_id),
                canonical_company_id=context["canonical_company_id"],
                source_digest=human.details["expected_source_digest"],
                source_version=human.details["expected_source_digest"],
                context_digest=human.details["expected_context_digest"],
                provenance="human",
                assessed_at=human.created_at.isoformat(),
                freshness="current",
            )
            if (
                value["source_digest"] != context["source_digest"]
                or value["context_digest"] != context["context_digest"]
            ):
                value["freshness"] = "stale"
            if context["source_conflict"]:
                value["freshness"] = "conflict"
            return {
                "assessment": value,
                "audit_id": str(human.id),
                "revision": 0,
                "override_revision": override_revision,
            }
        audit = chain_head(rows)
        if audit is None and context["source_conflict"]:
            raise KPIConflict("Conflicting source links")
    except KPIConflict:
        return {
            "assessment": {
                "rule_version": RULE,
                "activity_id": context["activity_id"],
                "canonical_company_id": context["canonical_company_id"],
                "source_digest": context["source_digest"],
                "source_version": context["source_version"],
                "context_digest": context["context_digest"],
                "relevant": "unknown",
                "phone_attempt_kind": "unknown",
                "eligibility": "eligible",
                "exclusion_reason": None,
                "reason": "Conflicting audit chain requires review",
                "evidence_refs": [],
                "provenance": "inferred",
                "assessed_at": datetime.now(UTC).isoformat(),
                "freshness": "conflict",
                "history_coverage": context["history_coverage"],
            },
            "audit_id": None,
            "revision": 0,
        }
    if audit is None:
        return {
            "assessment": None,
            "audit_id": None,
            "revision": 0,
            "override_revision": override_revision,
        }
    value = dict(audit.details["assessment"])
    if value.get("rule_version") != RULE or any(
        value[key] != context[key]
        for key in (
            "source_digest",
            "source_version",
            "context_digest",
            "canonical_company_id",
        )
    ):
        value["freshness"] = "stale"
    if context["source_conflict"]:
        value["freshness"] = "conflict"
    return {
        "assessment": value,
        "audit_id": str(audit.id),
        "revision": audit.details["revision"],
        "override_revision": override_revision,
    }


def period_bounds(anchor_date, period):
    if period == "day":
        start, end = anchor_date, anchor_date + timedelta(days=1)
    elif period == "week":
        start = anchor_date - timedelta(days=anchor_date.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = anchor_date.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        raise ValueError("Unsupported calendar period")
    return tuple(
        datetime.combine(day, time.min, ZoneInfo("Europe/Lisbon")).astimezone(UTC)
        for day in (start, end)
    )


def aggregate(
    session, workspace_id, *, anchor_date, period="day", legacy_anchor_day_attempts=0
):
    """Fold current effective events, never add reconciliation snapshots."""
    start, end = period_bounds(anchor_date, period)
    week_start, week_end = period_bounds(anchor_date, "week")
    day_start, day_end = period_bounds(anchor_date, "day")
    index = SourceIndex(session, workspace_id)
    relevance = dict.fromkeys(
        ("confirmed", "no", "unknown", "pending", "stale", "conflict"), 0
    )
    attempts = dict.fromkeys(("total", "new", "follow_up", "unknown"), 0)
    reasons = dict.fromkeys(
        (
            "non_phone",
            "test_scaffold",
            "voided",
            "cancelled_before_occurrence",
            "duplicate",
            "superseded",
        ),
        0,
    )
    history = dict.fromkeys(
        ("complete", "prior_proved", "incomplete", "ambiguous_order"), 0
    )
    processing = dict.fromkeys(("current", "pending", "stale", "conflict", "failed"), 0)
    assessed = []
    weekly_confirmed, eligible_anchor, exceptions = 0, 0, 0
    for row in index.activities.values():
        if row.activity_type != "call" or not (
            min(start, week_start, day_start)
            <= row.occurred_at
            < max(end, week_end, day_end)
        ):
            continue
        context = index.context(row.id)
        value = effective(session, workspace_id, row.id, index=index)["assessment"]
        freshness = value["freshness"] if value else "pending"
        exclusion = context["exclusion_reason"] or (
            value["exclusion_reason"] if value and freshness == "current" else None
        )
        in_period = start <= row.occurred_at < end
        if exclusion:
            if in_period:
                reasons[exclusion] += 1
            continue
        confirmed = bool(
            value and freshness == "current" and value["relevant"] == "yes"
        )
        if week_start <= row.occurred_at < week_end:
            weekly_confirmed += confirmed
        if day_start <= row.occurred_at < day_end:
            eligible_anchor += 1
        if not in_period:
            continue
        attempts["total"] += 1
        kind = (
            value["phone_attempt_kind"]
            if value and freshness == "current"
            else context["phone_attempt_kind"]
        )
        attempts[kind] += 1
        relevance[
            "confirmed"
            if confirmed
            else "no"
            if value and freshness == "current" and value["relevant"] == "no"
            else "unknown"
        ] += 1
        processing[freshness] += 1
        if (
            freshness == "pending"
            and semantic_hash(
                [
                    context[key]
                    for key in ("activity_id", "source_digest", "context_digest")
                ]
            )
            in index.failed_inputs
        ):
            processing["failed"] += 1
        if freshness != "current":
            relevance[freshness] += 1
        coverage = (
            value["history_coverage"]
            if value and freshness == "current"
            else context["history_coverage"]
        )
        history[
            {
                "complete_no_prior_attempt": "complete",
                "prior_attempt_proved": "prior_proved",
            }.get(coverage["state"], coverage["state"])
        ] += 1
        if value and freshness == "current":
            assessed.append(
                datetime.fromisoformat(value["assessed_at"].replace("Z", "+00:00"))
            )
        if row.actor_type in {"import", "migration", "system"} or not row.summary:
            exceptions += 1
    source_coverage = {"from": None, "through": None, "complete": False}
    last_run_id = None
    runs = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "workspace",
                AuditEvent.action == "commercial_kpi.reconciled",
                AuditEvent.details["status"].astext == "succeeded",
            )
        )
    )
    for audit in sorted(
        runs,
        key=lambda audit: datetime.fromisoformat(audit.details["finished_at"]),
        reverse=True,
    ):
        receipt = audit.details
        coverage_start = datetime.fromisoformat(receipt["coverage_from"])
        coverage_end = datetime.fromisoformat(receipt["coverage_through"])
        if not coverage_start <= day_start < coverage_end:
            continue
        manifest = [
            [context[key] for key in ("activity_id", "source_digest", "context_digest")]
            for row in sorted(index.activities.values(), key=lambda row: row.id)
            if row.activity_type == "call"
            and coverage_start <= row.occurred_at < coverage_end
            for context in [index.context(row.id)]
        ]
        source_coverage = {
            "from": receipt["coverage_from"],
            "through": receipt["cutoff"],
            "complete": coverage_start <= start
            and end <= coverage_end
            and semantic_hash(manifest) == receipt["input_watermark"],
        }
        last_run_id = receipt["run_id"]
        break
    return {
        "rule_version": RULE,
        "period": {
            "kind": period,
            "anchor_date": anchor_date.isoformat(),
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "timezone": "Europe/Lisbon",
        },
        "relevance": relevance,
        "phone_attempts": attempts,
        "exclusions": {"total": sum(reasons.values()), "by_reason": reasons},
        "coverage": {
            "history": history,
            "processing": processing,
            "source": source_coverage,
            "last_run_id": last_run_id,
            "next_reconciliation_at": None,
        },
        "weekly_goal": {
            "target": 50,
            "week_start": week_start.isoformat(),
            "week_end_exclusive": week_end.isoformat(),
            "confirmed": weekly_confirmed,
        },
        "drift": {
            "legacy_anchor_day_attempts": legacy_anchor_day_attempts,
            "eligible_anchor_day_attempts": eligible_anchor,
            "anchor_day_delta": eligible_anchor - legacy_anchor_day_attempts,
            "invalidated": relevance["stale"] + relevance["conflict"],
            "recorded_attempt_exceptions": exceptions,
        },
        "assessed_at": min(assessed).isoformat() if assessed else None,
        "generated_at": datetime.now(UTC).isoformat(),
    }
