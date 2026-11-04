"""Canonical ordinary-FU service; review/receipt authority remains server-owned."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.crm.persistence.models import AgentWork, Lead, Task
from src.crm.services.agent_work_service import (
    WorkConflict,
    enqueue_work,
    locked_work,
    validate_lease,
)
from src.crm.services.followup_context import collect_context, digest
from src.crm.services.followup_policy import POLICY_ID, CADENCE_VERSION, MAX_STAGE, INTERVAL_DAYS, due_at, obligation_key, task_id
from src.crm.services.followup_review import (
    FollowupError,
    audit,
    find_request,
    issue_request,
    ledger_name,
    save_ledger,
    timestamp,
    utc,
)
from src.crm.services.google_observation_service import apply_observation
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows


def mailbox_scope(principal, mailbox):
    if "mailbox:" + mailbox not in principal.source_scopes:
        raise FollowupError("mailbox_forbidden", 403)


def messages(context):
    return [m for t in context["provider"]["threads"] for m in t["messages"]]


class _VisibleReply(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.visible = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "blockquote"}:
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "blockquote"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.visible.append(data)


def reply_has_text(message):
    if (message.get("plain") or "").strip():
        return True
    parser = _VisibleReply()
    parser.feed(message.get("html") or "")
    return bool(parser.visible)


def verified_anchor(context, message_id):
    found = [m for m in messages(context) if m["id"] == message_id]
    if len(found) != 1:
        raise FollowupError("anchor_identity_ambiguous")
    anchor = found[0]
    if (
        "SENT" not in anchor["labels"]
        or "DRAFT" in anchor["labels"]
        or anchor["from"] != [context["provider"]["mailbox"]]
        or not anchor["to"]
        or not anchor["headers"].get("message-id")
        or timestamp(anchor["sent_at"]) > datetime.now(UTC)
    ):
        raise FollowupError("anchor_not_verified_sent")
    # Match CRM identity using the same normalization as contact ingestion.
    # Preserve every literal provider/header address for evidence and MIME.
    from src.crm.services.account_service import normalize_email
    def identities(values):
        return {normalize_email(value) for value in values if value}
    destinations=identities(anchor["to"] + anchor["cc"])
    candidates = []
    for lead in context["crm"]["lead"]["related_leads"]:
        emails = {lead.get("contact_email")}
        emails.update(c["primary_email"] for c in context["crm"]["contacts"]
            if c["id"] == lead.get("contact_id") or (
                lead.get("account_id") and c.get("account_id") == lead["account_id"]))
        if identities(emails).intersection(destinations):
            candidates.append(lead["id"])
    selected = context["crm"]["lead"]
    if candidates != [selected["id"]]:
        accounts = {lead.get("account_id") for lead in selected["related_leads"]
                    if lead["id"] in candidates}
        # The context request explicitly selects one existing Lead. Shared
        # contacts of that exact Account are valid; cross-account guesses are not.
        if not (selected["id"] in candidates and selected.get("account_id")
                and accounts == {selected["account_id"]}):
            raise FollowupError("anchor_lead_mapping_ambiguous")
    if not destinations <= identities(context["recipients"]):
        raise FollowupError("anchor_recipient_mapping_incomplete")
    return anchor


def assessment(context, anchor, *, own_task_id=None):
    crm = context["crm"]

    def hold(reason, disposition="deferred"):
        return {
            "disposition": disposition,
            "reason_code": reason,
            "not_before": None,
            "evidence_refs": [],
        }

    leads = crm["lead"]["related_leads"]
    if any(l["stage"] in {"lost", "not_a_fit", "won"} for l in leads):
        return hold("closed_or_suppressed", "suppressed")
    if any(c["status"] != "active" for c in crm["contacts"]):
        return hold("contact_inactive", "suppressed")
    if any(
        (s.get("metadata_json") or {}).get("suppressed") is True
        for s in crm["lead"]["sources"]
    ):
        return hold("source_suppressed", "suppressed")
    names = " ".join(l.get("company_name") or "" for l in leads).casefold()
    if "cubotonic" in names or "precisão laser" in names or "precisao laser" in names:
        return hold("explicit_company_exclusion", "suppressed")
    # Calls, opportunities and booked meetings do not pause the email sequence.
    # Proposal cadence remains outside this ordinary-email change.
    if crm["account"] and crm["account"]["proposals"]:
        return hold("proposal_context_requires_review", "needs_context")
    if any(a.get("outcome_code") == "not_interested"
           and a.get("actor_type") not in {"agent", "automation", "system", "service"}
           and a.get("activity_type") in {"note", "call", "email_received"}
           and not any(b.get("supersedes_activity_id") == a.get("id") for b in crm["activities"])
           for a in crm["activities"]):
        return hold("explicit_refusal", "suppressed")
    not_before = None
    for profile in crm.get("commercial_profiles", []):
        field = profile.get("fields", {}).get("next_contact")
        if not field or field.get("value") is None:
            continue
        if not field.get("current_entity"):
            return hold("next_contact_review_required", "needs_context")
        route = field["value"]
        if route["category"] == "closed":
            return hold("explicit_contact_stop", "suppressed")
        if route["category"] in {"await_client", "contact_validation"}:
            return hold("explicit_wait_or_contact_review", "needs_context")
        # A future phone callback constrains CALL, never email.
        if route["channel"] == "email" and route.get("not_before"):
            date = timestamp(route["not_before"])
            not_before = max(not_before, date) if not_before else date

    def candidate(reason):
        value = hold(reason, "eligible")
        value["not_before"] = utc(not_before) if not_before else None
        return value
    if context["provider"]["direct_drafts"]:
        return hold("existing_direct_draft")
    newer = [m for m in messages(context)
             if m["id"] != anchor["id"]
             and m["internal_date_ms"] >= anchor["internal_date_ms"]]
    human_reply = False
    automatic_reply = False
    for msg in newer:
        headers = msg.get("headers") or {}
        sender = msg.get("from") or []
        recipient = (msg.get("to") or []) + (msg.get("cc") or [])
        if ("SENT" in msg.get("labels", []) or "DRAFT" in msg.get("labels", [])
                or len(sender) != 1 or sender[0] not in context["recipients"]
                or context["provider"]["mailbox"] not in recipient):
            return hold("newer_message_requires_reconciliation")
        auto = (headers.get("auto-submitted", "").strip().casefold() not in {"", "no"}
                or "x-autoreply" in headers or "x-autorespond" in headers
                or "list-id" in headers
                or headers.get("precedence", "").strip().casefold() in {"bulk", "list", "junk"}
                or any(marker in (msg.get("plain") or "").casefold() for marker in (
                    "out of office", "automatic reply", "resposta automática", "fora do escritório",
                )))
        if auto:
            automatic_reply = True
        elif reply_has_text(msg):
            human_reply = True
        else:
            return hold("newer_message_requires_reconciliation")
    if human_reply:
        # Keep the lead visible as a contextual action, not "finished" and not a
        # blind reminder. Existing owner/review flow must resolve the question.
        value = hold("human_reply_contextual_action", "needs_context")
        value["next_action"] = "Reler e responder à mensagem humana; não enviar reminder genérico"
        value["evidence_refs"] = [m["id"] for m in newer]
        return value
    if automatic_reply:
        return hold("automatic_reply_requires_reconciliation")
    email_obligations = [t for t in crm["tasks_all_statuses"] if (
        t["status"] == "open" and t["id"] != own_task_id
        and t.get("task_type") == "email"
        and t.get("source_rule") in {None, POLICY_ID, "manual_next_action", "head_of_sales_note"})]
    if email_obligations:
        value = hold("human_or_other_obligation")
        value["evidence_refs"] = [t["id"] for t in email_obligations]
        dates = [timestamp(t["due_at"]) for t in email_obligations if t.get("due_at")]
        value["not_before"] = utc(max(dates)) if dates else None
        return value
    # Human commitments are reviewed in full, never guessed from a date keyword.
    if any(
        a["activity_type"] in {"note", "call", "meeting"}
        and timestamp(a["occurred_at"]) >= timestamp(anchor["sent_at"])
        for a in crm["activities"]
    ):
        # Candidate only: full human context must pass the independent exact-MIME
        # review and fresh-context execution gate. A note alone is not a pause.
        return candidate("human_context_requires_review")
    return candidate("ordinary_sequence_candidate")


def current_context(session, principal, lead_id, mailbox, provider, *, thread_ids=(), anchor_message_id=None):
    mailbox_scope(principal, mailbox)
    if anchor_message_id is not None:
        from urllib.parse import quote

        from src.crm.connectors.followup_email import message_dto

        direct = message_dto(provider.get("/messages/" + quote(anchor_message_id, safe=""), {"format": "raw"}))
        if direct["id"] != anchor_message_id:
            raise FollowupError("anchor_identity_ambiguous")
        thread_ids = (*thread_ids, direct["thread_id"])
    context = collect_context(
        session,
        principal.workspace_id,
        lead_id,
        mailbox,
        provider,
        thread_ids=thread_ids,
    )
    if anchor_message_id is not None:
        verified_anchor(context, anchor_message_id)
    return context


def audit_inventory(session, principal, mailbox, *, cursor=None, limit=50):
    from src.crm.persistence.models import SourceIdentity
    from src.crm.services.followup_context import dto, page_records, rows
    from src.crm.services.followup_review import public_payload

    mailbox_scope(principal, mailbox)
    sources = {
        str(s.id): dto(s) for s in rows(session, SourceIdentity, principal.workspace_id)
    }
    tasks = rows(session, Task, principal.workspace_id)
    work = rows(session, AgentWork, principal.workspace_id)
    records = []
    for lead in rows(session, Lead, principal.workspace_id):
        source = sources.get(str(lead.source_identity_id))
        records.append(
            {
                "id": str(lead.id),
                "lead_version": lead.version,
                "stage": lead.stage,
                "source": source,
                "lead_snapshot_hash": digest(dto(lead)),
                "tasks": [dto(t) for t in tasks if t.lead_id == lead.id],
                "work": [
                    {
                        "id": str(w.id),
                        "kind": w.kind,
                        "status": w.status,
                        "source_key": w.source_key,
                        "payload": public_payload(w.payload),
                        "result": w.result,
                        "updated_at": utc(w.updated_at),
                    }
                    for w in work
                    if w.lead_id == lead.id
                    and (
                        w.payload.get("followup_v1", {})
                        .get("obligation", {})
                        .get("mailbox")
                        == mailbox
                        or w.payload.get("followup_anchor_v1", {}).get("mailbox")
                        == mailbox
                    )
                ],
            }
        )
    return page_records(
        records,
        {
            "workspace": str(principal.workspace_id),
            "actor": str(principal.actor_id),
            "mailbox": mailbox,
            "kind": "followup_audit",
        },
        cursor=cursor,
        limit=limit,
    ) | {"gmail_audited": False}


def recover_expired_preparation(session, principal, work):
    """One bounded recovery after the verified 26/09 runtime repair.

    Never replay an attempted send, reviewed/rejected candidate, human waiting
    decision or a live lease. The normal preparation/review gates still apply.
    """
    name = "followup_anchor_v1" if "followup_anchor_v1" in work.payload else "followup_v1"
    state = deepcopy(work.payload.get(name) or {})
    # Audited preparation allowances belong to the explicit recover-claim path.
    # Preserve their consumed state and counters; generic reset cannot claim them.
    if (not state or work.status != "failed" or work.error != "Worker lease expired"
        or state.get("preparation_recovery") is not None
        or state.get("runtime_recovery_version") == CADENCE_VERSION
        or state.get("decision") or state.get("intent")
        or (state.get("review") or {}).get("requests")):
        return False
    state["runtime_recovery_version"] = CADENCE_VERSION
    save_ledger(work, state)
    work.status, work.attempts, work.error = "queued", 0, None
    work.available_at = datetime.now(UTC)
    work.lease_token = work.lease_until = None
    audit(session, work, principal.actor_id, "preparation_runtime_recovered",
          {"runtime_version": CADENCE_VERSION, "prior_reason": "Worker lease expired"})
    return True


def reconcile(session, principal, body, provider):
    _locked_company_lead(session, principal.workspace_id, body.lead_id)
    context = current_context(session, principal, body.lead_id, body.mailbox, provider,
                              anchor_message_id=body.anchor_message_id)
    if (
        context["context_hash"] != body.context_hash
        or context["coverage"]["coverage_id"] != body.coverage_id
        or context["crm"]["lead"]["version"] != body.expected_lead_version
    ):
        raise FollowupError("context_changed")
    anchor = verified_anchor(context, body.anchor_message_id)
    # This uses the existing provider identity/projection, never a second CRM.
    observed = apply_observation(
        session,
        principal.workspace_id,
        {
            "provider": "gmail",
            "source_scope": "mailbox:" + body.mailbox,
            "id": anchor["id"],
            "thread_id": anchor["thread_id"],
            "occurred_at": anchor["sent_at"],
            "direction": "outbound",
            "contact_emails": anchor["to"] + anchor["cc"],
            "subject": anchor["headers"].get("subject") or "Email enviado",
            "excerpt": anchor["plain"][:2000],
            "attachments": [],
        },
        rebind_unmatched_lead_id=body.lead_id, actor_id=principal.actor_id,
    )
    work = locked_work(session, principal.workspace_id, UUID(observed["id"]))
    if work.lead_id != body.lead_id or work.kind != "sent_review":
        raise FollowupError("anchor_work_mapping_conflict")
    previous = (work.payload.get("followup_anchor_v1") or {}).get("obligation")
    if assessment(context, anchor, own_task_id=(previous or {}).get("task_id"))["disposition"] == "eligible":
        recover_expired_preparation(session, principal, work)
        if previous and previous.get("work_id"):
            due_work = locked_work(session, principal.workspace_id, UUID(previous["work_id"]))
            recover_expired_preparation(session, principal, due_work)
    known = work.payload.get("followup_anchor_v1", {})
    from src.crm.services.followup_continuity import consolidate, reopen_unclassified
    reopen_unclassified(session, principal, work, context, anchor)
    known = work.payload.get("followup_anchor_v1", {})
    if known.get("classification") == "uncertain" and (known.get("decision") or {}).get(
        "context_hash"
    ) != digest(context):
        # A new source snapshot, not a new lease, permits reevaluation.
        known = deepcopy(known)
        known["classification"] = known["decision"] = None
        known["review"]["current_request_id"] = None
        known["assessment"] = {
            "disposition": "needs_context",
            "reason_code": "source_changed",
            "not_before": None,
            "evidence_refs": [],
        }
        save_ledger(work, known)
        work.status = "queued"
        work.attempts = 0
        work.available_at = datetime.now(UTC)
        work.lease_token = work.lease_until = None
        audit(
            session,
            work,
            principal.actor_id,
            "anchor_invalidated",
            {"reason": "source_changed"},
        )
    if (known.get("decision") or {}).get("purpose") == "own_sent_receipt":
        if known["decision"]["observation_version"] != digest(anchor):
            raise FollowupError("own_receipt_changed")
        from src.crm.services.followup_missing_effect import reconcile_alcont_successor
        effect = reconcile_alcont_successor(session, principal, work, context, anchor, body)
        continuity = consolidate(session, work, context, anchor,
            assessment(context, anchor, own_task_id=(known.get("obligation") or {}).get("task_id")),
            owner_id=principal.actor_id)
        return {
            "work_id": str(work.id),
            "disposition": "already_satisfied",
            "reason_code": "own_sent_receipt",
            "classification": known["classification"],
            "continuity": continuity,
            **effect,
        }
    decision = known.get("decision") or {}
    if (
        decision.get("purpose") == "anchor_classification"
        and decision.get("reviewer_receipt_id")
        and decision.get("observation_version") == digest(anchor)
        and known.get("classification") in {"intro", "fu1", "fu2", "fu3", "fu4"}
        and not known.get("obligation")
    ):
        fresh_assessment = assessment(context, anchor)
        known = deepcopy(known)
        known["assessment"] = fresh_assessment
        if fresh_assessment["disposition"] == "eligible":
            known["obligation"] = create_obligation(
                session, work.workspace_id,
                work.payload.get("owner_id") or principal.actor_id,
                context, anchor, {"intro": 1, "fu1": 2, "fu2": 3, "fu3": 4, "fu4": 5}[known["classification"]],
            )
        save_ledger(work, known)
    if "followup_anchor_v1" not in work.payload:
        if work.status == "completed":
            # Completed generic observation is not a completed anchor review.
            # Keep the original result for audit, reopen only this new purpose.
            work.status = "queued"
            work.attempts = 0
            work.available_at = datetime.now(UTC)
            work.lease_token = work.lease_until = None
        work.payload = deepcopy(work.payload) | {
            "followup_anchor_v1": {
                "schema": "ordinary-followup-anchor/v1",
                "purpose": "anchor_classification",
                "mailbox": body.mailbox,
                "lead_id": str(body.lead_id),
                "thread_id": anchor["thread_id"],
                "provider_message_id": anchor["id"],
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
        }
    known = work.payload.get("followup_anchor_v1") or {}
    continuity = consolidate(session, work, context, anchor,
        assessment(context, anchor, own_task_id=(known.get("obligation") or {}).get("task_id")),
        owner_id=principal.actor_id)
    session.flush()
    return {
        "work_id": str(work.id),
        "disposition": "reconciled" if continuity["closed"] else "pending_anchor_review"
            if continuity["state"] == "awaiting_review" else "pending_reconciliation",
        "reason_code": continuity["reason_code"],
        "continuity": continuity,
    }


def prepare_anchor(session, principal, work_id, body, provider, *, now=None):
    now = now or datetime.now(UTC)
    work = locked_work(session, principal.workspace_id, work_id)
    validate_lease(work, body.lease_token, now)
    if work.kind != "sent_review" or ledger_name(work) != "followup_anchor_v1":
        raise FollowupError("not_anchor_work")
    state = work.payload["followup_anchor_v1"]
    _locked_company_lead(session, principal.workspace_id, work.lead_id)
    context = current_context(
        session,
        principal,
        work.lead_id,
        state["mailbox"],
        provider,
        thread_ids=[state["thread_id"]],
    )
    if (
        context["context_hash"] != body.context_hash
        or context["crm"]["lead"]["version"] != body.expected_lead_version
    ):
        raise FollowupError("context_changed")
    anchor = verified_anchor(context, state["provider_message_id"])
    proposal = body.proposal.model_dump()
    index = {m["id"]: m for m in messages(context)}
    if (
        len(set(proposal["supporting_message_ids"]))
        != len(proposal["supporting_message_ids"])
        or anchor["id"] not in proposal["supporting_message_ids"]
        or not set(proposal["supporting_message_ids"]) <= index.keys()
    ):
        raise FollowupError("classification_evidence_missing")
    for quote in proposal["evidence_quotes"]:
        source = index.get(quote["message_id"])
        if not source or quote["quote"] not in source["plain"] + source["html"]:
            raise FollowupError("classification_quote_unverified")
    package = {
        "purpose": "anchor_classification",
        "schema": "ordinary-followup-anchor/v1",
        "policy_id": POLICY_ID,
        "mailbox": state["mailbox"],
        "lead_id": str(work.lead_id),
        "thread_id": anchor["thread_id"],
        "provider_message_id": anchor["id"],
        "obligation_key": None,
        "expected_lead_version": body.expected_lead_version,
        "expected_task_version": None,
        "observation_version": digest(anchor),
        "context": context,
        "context_hash": digest(context),
        "proposal": proposal,
        "payload_sha256": digest(
            {"purpose": "anchor_classification", "anchor": anchor, "proposal": proposal}
        ),
        "signature_sha256": hashlib.sha256(
            context["provider"]["send_as"]["signature"].encode()
        ).hexdigest(),
    }
    return issue_request(session, work, package, body.lease_token, now)


def create_obligation(session, workspace, actor, context, anchor, stage):
    lead = session.get(Lead, UUID(context["crm"]["lead"]["id"]))
    assessment_value = assessment(context, anchor)
    not_before = assessment_value.get("not_before")
    key = obligation_key(
        workspace,
        context["provider"]["mailbox"],
        lead.id,
        anchor["thread_id"],
        stage,
        anchor["id"],
    )
    identifier = task_id(workspace, key)
    session.execute(
        insert(Task)
        .values(
            id=identifier,
            workspace_id=workspace,
            lead_id=lead.id,
            account_id=lead.account_id,
            task_type="email",
            source_rule=POLICY_ID,
            title=("Enviar último follow-up" if stage == 5 else f"Enviar {stage}.º follow-up"),
            due_at=due_at(timestamp(anchor["sent_at"]), stage,
                          not_before=timestamp(not_before) if not_before else None),
            owner_user_id=actor,
        )
        .on_conflict_do_nothing(index_elements=[Task.id])
    )
    task = session.get(Task, identifier)
    obligation = {
        "key": key,
        "mailbox": context["provider"]["mailbox"],
        "lead_id": str(lead.id),
        "thread_id": anchor["thread_id"],
        "fu_stage": stage,
        "anchor_message_id": anchor["id"],
        "anchor_sent_at": anchor["sent_at"],
    }
    work_id = enqueue_work(
        session,
        workspace_id=workspace,
        source_key=key,
        kind="followup_due",
        lead_id=lead.id,
        task_id=identifier,
        available_at=task.due_at,
        payload={
            "task_version": task.version,
            "followup_v1": {
                "obligation": obligation,
                "task_version": task.version,
                "expected_lead_version": lead.version,
                "policy_id": POLICY_ID,
                "decision": None,
                "intent": None,
                "assessment": assessment(context, anchor),
                "review": {"current_request_id": None, "requests": []},
            },
        },
    )
    return {"task_id": str(identifier), "work_id": str(work_id), "obligation_key": key}


def payload_context(session, principal, work, provider, *, now=None, fresh_preparation=False):
    state = work.payload["followup_v1"]
    obligation = state["obligation"]
    # A scheduled obligation may predate harmless Lead edits by days. Only its
    # first preparation may bind the fresh source after the caller's exact CAS.
    # Once reviewed, every subsequent context/execute check remains pinned.
    unprepared = (fresh_preparation and state.get("intent") is None
                  and not state.get("decision")
                  and not (state.get("review") or {}).get("requests"))
    _locked_company_lead(session, work.workspace_id, work.lead_id)
    _locked_rows(
        session,
        select(Task).where(
            Task.workspace_id == work.workspace_id, Task.id == work.task_id
        ),
    )
    snapshot = current_context(
        session,
        principal,
        work.lead_id,
        obligation["mailbox"],
        provider,
        thread_ids=[obligation["thread_id"]],
    )
    anchor = verified_anchor(snapshot, obligation["anchor_message_id"])
    task = session.get(Task, work.task_id)
    if (
        task is None
        or task.status != "open"
        or task.source_rule != POLICY_ID
        or task.version != state["task_version"]
        or (not unprepared and
            snapshot["crm"]["lead"]["version"] != state["expected_lead_version"])
    ):
        raise FollowupError("task_or_lead_changed")
    assessment_value = assessment(snapshot, anchor, own_task_id=str(task.id))
    if assessment_value["disposition"] != "eligible":
        raise FollowupError(assessment_value["reason_code"])
    if assessment_value.get("not_before") and timestamp(assessment_value["not_before"]) > (now or datetime.now(UTC)):
        raise FollowupError("explicit_email_not_before")
    signature_hash = hashlib.sha256(
        snapshot["provider"]["send_as"]["signature"].encode()
    ).hexdigest()
    context = {
        "schema": "followup-context/v1.1",
        "obligation": obligation,
        "source_versions": snapshot["source_versions"]
        | {
            "task_version": task.version,
            "policy_version": POLICY_ID,
            "signature_sha256": signature_hash,
        },
        "coverage": snapshot["coverage"],
        "crm": snapshot["crm"],
        "provider": snapshot["provider"],
        "assessment": assessment_value,
        "policy": {
            "id": POLICY_ID,
            "fu_max": MAX_STAGE,
            "cadence_version": CADENCE_VERSION,
            "interval_days": list(INTERVAL_DAYS),
            "final_interval_calendar_months": 1,
            "timezone": "Europe/Lisbon",
            "calendar_policy": "calendar-24x7-2026-10-07",
            "slot_hours": list(range(24)),
            "slot_weekdays": list(range(7)),
            "minimum_sent_spacing_seconds": 600,
            "allow_initial": False,
            "allow_reactivation": False,
            "allow_final_sequence_reapproach": True,
            "allow_financial_commitment": False,
            "context_rule": "Calls/callbacks/booked meetings alone do not pause email. Acknowledge existing agreements, never reinvite. Explicit refusals, pauses, agreed email dates and human questions override cadence; no blind reminder. FU5 closes sequence only, never commercial stage.",
        },
    }
    return context, snapshot, anchor, task


def prepare_payload(session, principal, work_id, body, provider, *, now=None):
    from src.crm.connectors.followup_email import validate_payload

    now = now or datetime.now(UTC)
    work = locked_work(session, principal.workspace_id, work_id)
    validate_lease(work, body.lease_token, now)
    if work.kind != "followup_due" or ledger_name(work) != "followup_v1":
        raise FollowupError("not_followup_send_work")
    if work.payload["followup_v1"]["intent"] is not None:
        raise FollowupError("intent_requires_recovery")
    context, snapshot, anchor, task = payload_context(
        session, principal, work, provider, now=now, fresh_preparation=True
    )
    if (
        body.context_hash != snapshot["context_hash"]
        or body.expected_task_version != task.version
        or body.expected_lead_version != snapshot["crm"]["lead"]["version"]
    ):
        raise FollowupError("context_changed")
    raw = validate_payload(body.payload_raw_b64, context, context["obligation"], anchor)
    if work.payload["followup_v1"]["expected_lead_version"] != body.expected_lead_version:
        state = deepcopy(work.payload["followup_v1"])
        state["expected_lead_version"] = body.expected_lead_version
        save_ledger(work, state)
    package = {
        "policy_id": POLICY_ID,
        "mailbox": context["obligation"]["mailbox"],
        "obligation_key": work.source_key,
        "expected_lead_version": body.expected_lead_version,
        "expected_task_version": body.expected_task_version,
        "context": context,
        "context_hash": digest(context),
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
        "payload_raw_b64": body.payload_raw_b64,
        "signature_sha256": context["source_versions"]["signature_sha256"],
    }
    return issue_request(session, work, package, body.lease_token, now)


def apply_manual_receipt(session, work, context, anchor, classification):
    """A reviewed manual FU may satisfy only its unchanged exact auto Task."""
    if classification not in {"fu1", "fu2", "fu3", "fu4", "fu5"}:
        return False
    from src.crm.persistence.models import Activity, SourceIdentity

    candidates = []
    for row in session.scalars(
        select(AgentWork).where(
            AgentWork.workspace_id == work.workspace_id,
            AgentWork.lead_id == work.lead_id,
            AgentWork.kind == "followup_due",
        )
    ):
        state = row.payload.get("followup_v1", {})
        obligation = state.get("obligation", {})
        if (
            obligation.get("mailbox") == context["provider"]["mailbox"]
            and obligation.get("thread_id") == anchor["thread_id"]
            and obligation.get("fu_stage") == int(classification[-1])
            and state.get("intent") is None
            and timestamp(obligation["anchor_sent_at"]) < timestamp(anchor["sent_at"])
        ):
            predecessor = verified_anchor(context, obligation["anchor_message_id"])
            if predecessor["to"] == anchor["to"] and predecessor["cc"] == anchor["cc"]:
                candidates.append(row.id)
    if len(candidates) != 1:
        return False
    prior = locked_work(session, work.workspace_id, candidates[0])
    state = prior.payload["followup_v1"]
    _locked_rows(
        session,
        select(Task).where(
            Task.id == prior.task_id, Task.workspace_id == work.workspace_id
        ),
    )
    task = session.get(Task, prior.task_id)
    if (
        task.status != "open"
        or task.source_rule != POLICY_ID
        or task.version != state["task_version"]
        or state["intent"] is not None
    ):
        return False
    activity = session.scalar(
        select(Activity)
        .join(SourceIdentity, Activity.source_identity_id == SourceIdentity.id)
        .where(
            Activity.workspace_id == work.workspace_id,
            Activity.lead_id == work.lead_id,
            SourceIdentity.workspace_id == work.workspace_id,
            SourceIdentity.source_system == "gmail",
            SourceIdentity.source_scope == "mailbox:" + context["provider"]["mailbox"],
            SourceIdentity.entity_kind == "message",
            SourceIdentity.external_id == anchor["id"],
        )
    )
    if activity is None:
        raise FollowupError("manual_receipt_identity_missing")
    task.status, task.completed_at, task.completion_activity_id = (
        "completed",
        timestamp(anchor["sent_at"]),
        activity.id,
    )
    prior.status = "completed"
    prior.last_lease_token = prior.lease_token
    prior.lease_token = prior.lease_until = None
    prior.result = {
        "disposition": "already_satisfied",
        "reason_code": "reviewed_manual_sent",
        "provider_message_id": anchor["id"],
        "activity_id": str(activity.id),
        "anchor_work_id": str(work.id),
    }
    prior.result_hash = digest(prior.result)
    session.flush()
    return True


def accept_verdict(session, principal, request_id, body, provider, *, now=None):
    now = now or datetime.now(UTC)
    work, item = find_request(session, principal, request_id, lock=True)
    package = item["package"]
    if item["consumed_at"] is not None:
        raise FollowupError("review_consumed")
    if timestamp(package["expires_at"]) <= now:
        raise FollowupError("review_expired", 410)
    if (
        item["status"] != "pending_review"
        or item["config_hash"] != principal.config_hash
    ):
        raise FollowupError("review_invalidated")
    if (
        body.request_hash != item["request_hash"]
        or digest(package) != item["request_hash"]
        or any(
            getattr(body, k) != package[k]
            for k in ("nonce", "context_hash", "payload_sha256", "signature_sha256")
        )
    ):
        raise FollowupError("review_binding_mismatch")
    validate_lease(work, UUID(item["lease_token"]), now)
    _locked_company_lead(session, principal.workspace_id, work.lead_id)
    state = deepcopy(work.payload[ledger_name(work)])
    is_anchor = ledger_name(work) == "followup_anchor_v1"
    if is_anchor:
        if package.get("purpose") != "anchor_classification":
            raise FollowupError("review_purpose_mismatch")
        context = current_context(
            session,
            principal,
            work.lead_id,
            package["mailbox"],
            provider,
            thread_ids=[package["thread_id"]],
        )
        anchor = verified_anchor(context, package["provider_message_id"])
        if digest(anchor) != package["observation_version"]:
            raise FollowupError("anchor_changed")
    else:
        if "purpose" in package or package.get("obligation_key") != work.source_key:
            raise FollowupError("review_purpose_mismatch")
        context, _snapshot, anchor, _task = payload_context(
            session, principal, work, provider, now=now
        )
        from src.crm.connectors.followup_email import validate_payload

        raw = validate_payload(
            package["payload_raw_b64"], context, context["obligation"], anchor
        )
        if hashlib.sha256(raw).hexdigest() != body.payload_sha256:
            raise FollowupError("payload_changed")
    if digest(context) != package["context_hash"]:
        raise FollowupError("context_changed")
    verdict = body.verdict.model_dump() if body.verdict else None
    approved = body.runner_outcome == "verdict" and verdict["decision"] == "APPROVE"
    reason = (
        "review_approved"
        if approved
        else "review_owner_required"
        if verdict and verdict["decision"] == "JOSE_REQUIRED"
        else "review_rejected"
        if body.runner_outcome == "verdict"
        else "review_" + body.runner_outcome
    )
    receipt = {
        "id": str(uuid4()),
        "authenticated_actor_id": str(principal.actor_id),
        "received_at": utc(now),
        "runner_outcome": body.runner_outcome,
        "verdict": verdict,
        "reviewer_session_id": body.reviewer_session_id,
    }
    item.update(
        status="approved" if approved else "rejected",
        consumed_at=utc(now),
        receipt=receipt,
    )
    state["review"]["requests"] = [
        item if i["package"]["request_id"] == str(request_id) else i
        for i in state["review"]["requests"]
    ]
    if approved and not is_anchor:
        state["decision"] = {
            "context_hash": body.context_hash,
            "source_versions": context["source_versions"],
            "coverage_id": context["coverage"]["coverage_id"],
            "payload_sha256": body.payload_sha256,
            "signature_sha256": body.signature_sha256,
            "reviewer_receipt_id": receipt["id"],
            "reviewed_at": utc(now),
            "expires_at": package["expires_at"],
            "review_request_id": str(request_id),
            "request_hash": item["request_hash"],
        }
        state["assessment"] = context["assessment"]
    elif approved:
        state["classification"] = package["proposal"]["classification"]
        state["decision"] = {
            "purpose": "anchor_classification",
            "review_request_id": str(request_id),
            "request_hash": item["request_hash"],
            "reviewer_receipt_id": receipt["id"],
            "context_hash": body.context_hash,
            "observation_version": package["observation_version"],
            "reviewed_at": utc(now),
            "expires_at": package["expires_at"],
        }
        if apply_manual_receipt(
            session, work, context, anchor, state["classification"]
        ):
            context = current_context(
                session,
                principal,
                work.lead_id,
                package["mailbox"],
                provider,
                thread_ids=[anchor["thread_id"]],
            )
        state["assessment"] = assessment(context, anchor)
        klass = state["classification"]
        if (
            klass in {"intro", "fu1", "fu2", "fu3", "fu4"}
            and state["assessment"]["disposition"] == "eligible"
        ):
            state["obligation"] = create_obligation(
                session,
                work.workspace_id,
                work.payload.get("owner_id") or principal.actor_id,
                context,
                anchor,
                {"intro": 1, "fu1": 2, "fu2": 3, "fu3": 4, "fu4": 5}[klass],
            )
        elif klass in {"fu5", "non_sequence", "uncertain"}:
            state["assessment"] = {
                "disposition": "sequence_finished"
                if klass == "fu5"
                else "suppressed"
                if klass == "non_sequence"
                else "needs_context",
                "reason_code": "anchor_" + klass,
                "not_before": None,
                "evidence_refs": package["proposal"]["supporting_message_ids"],
            }
    else:
        state["assessment"] = {
            "disposition": "deferred",
            "reason_code": reason,
            "not_before": None,
            "evidence_refs": [],
        }
    state["last_evaluated_at"] = utc(now)
    if is_anchor and approved:
        # Complete the semantic review vehicle, never an email Task without SENT.
        work.status = (
            "waiting" if state["classification"] == "uncertain" else "completed"
        )
        work.last_lease_token = work.lease_token
        work.lease_token = work.lease_until = None
        work.result = {
            "disposition": state["assessment"]["disposition"],
            "reviewer_receipt_id": receipt["id"],
            "classification": state["classification"],
        }
        work.result_hash = digest(work.result)
    save_ledger(work, state)
    audit(
        session,
        work,
        principal.actor_id,
        "review_consumed",
        {
            "request_id": str(request_id),
            "receipt_id": receipt["id"],
            "reason_code": reason,
        },
    )
    if is_anchor and approved:
        from src.crm.services.followup_projection import persist
        persist(session, work, context, anchor, state["classification"], state["assessment"])
        from src.crm.services.followup_continuity import consolidate
        consolidate(session, work, context, anchor, state["assessment"],
                    owner_id=principal.actor_id, now=now)
    session.flush()
    return {
        "request_id": str(request_id),
        "work_id": str(work.id),
        "reviewer_receipt_id": receipt["id"],
        "status": item["status"],
        "reason_code": reason,
        "context_hash": body.context_hash,
        "payload_sha256": body.payload_sha256,
        "expires_at": package["expires_at"],
    }


def approved_payload(session, principal, work, body, provider, now):
    from src.crm.connectors.followup_email import validate_payload
    from src.crm.services.followup_review import review_configuration

    configured, _ = review_configuration()
    state = work.payload["followup_v1"]
    decision = state["decision"]
    recovery = state.get('preparation_recovery')
    if recovery and (recovery.get('status') != 'consumed' or not decision
                     or decision.get('review_request_id') in recovery['prior_request_ids']):
        raise FollowupError('fresh_recovery_review_required')
    if not decision or decision["reviewer_receipt_id"] != str(body.reviewer_receipt_id):
        raise FollowupError("review_receipt_required")
    item = next(
        (
            r
            for r in state["review"]["requests"]
            if r["package"]["request_id"] == decision["review_request_id"]
        ),
        None,
    )
    if (
        item is None
        or item["status"] != "approved"
        or item["config_hash"] != configured.config_hash
        or timestamp(decision["expires_at"]) <= now
        or item["lease_token"] != str(body.lease_token)
    ):
        raise FollowupError("review_invalidated")
    validate_lease(work, body.lease_token, now)
    context, _snapshot, anchor, task = payload_context(
        session, principal, work, provider, now=now
    )
    from src.crm.services.followup_expired_handoff import require_successor
    require_successor(state, item, decision, body.lease_token)
    if (
        digest(context) != body.context_hash
        or body.context_hash != decision["context_hash"]
        or body.payload_sha256 != decision["payload_sha256"]
        or body.expected_task_version != task.version
        or body.expected_lead_version != context["source_versions"]["lead_version"]
        or digest(item["package"]) != item["request_hash"]
    ):
        raise FollowupError("review_context_changed")
    raw = validate_payload(
        item["package"]["payload_raw_b64"], context, state["obligation"], anchor
    )
    if hashlib.sha256(raw).hexdigest() != body.payload_sha256:
        raise FollowupError("review_payload_changed")
    if max(task.due_at, due_at(timestamp(anchor["sent_at"]), state["obligation"]["fu_stage"])) > now:
        raise FollowupError("followup_not_due")
    return raw, item


class SpacingPending(FollowupError):
    def __init__(self, not_before):
        super().__init__("sent_spacing_pending")
        self.not_before = not_before


def defer_spacing(session, principal, work, not_before):
    """A known no-send is requeued, never confused with an ambiguous POST."""
    state = deepcopy(work.payload["followup_v1"])
    if state["intent"] is not None:
        raise FollowupError("intent_requires_recovery")
    state["decision"] = None
    state["review"]["current_request_id"] = None
    exact_not_before = not_before.astimezone(UTC).isoformat().replace("+00:00", "Z")
    state["assessment"] = {
        "disposition": "deferred", "reason_code": "sent_spacing_pending",
        "not_before": exact_not_before, "evidence_refs": [],
    }
    save_ledger(work, state)
    work.status = "failed" if state.get('preparation_recovery') else "queued"
    work.available_at = not_before
    if not state.get('preparation_recovery'):
        work.attempts = 0
    work.last_lease_token = work.lease_token
    work.lease_token = work.lease_until = None
    audit(session, work, principal.actor_id, "spacing_deferred", {
        "not_before": utc(not_before), "post_attempted": False,
    })
    return {"work_id": str(work.id), "intent_state": None,
            **state["assessment"]}


def reserve_slot(session, work, now):
    from sqlalchemy import text

    from src.crm.services.followup_policy import LISBON

    # Jose HQ 19Oct: sole sender cannot start before 20Oct08h Lisbon.
    start = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
    if now < start:
        raise SpacingPending(start)
    local = now.astimezone(LISBON)
    mailbox = work.payload["followup_v1"]["obligation"]["mailbox"]
    lock = int.from_bytes(
        hashlib.sha256(("ordinary-followup-mailbox:" + mailbox).encode()).digest()[:8],
        "big",
        signed=True,
    )
    # Transaction-scoped, parameterized mailbox lock. Work->mailbox->company
    # NOWAIT company locks avoid reverse-order deadlock with human writers.
    if not session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock}
    ):
        raise FollowupError("mailbox_busy")
    day_key = f"{work.workspace_id}:{mailbox}:{local.date().isoformat()}"
    slot_key = day_key + f":{local.hour:02d}"
    intents = []
    for row in session.scalars(
        select(AgentWork).where(
            AgentWork.kind == "followup_due",
            AgentWork.payload["followup_v1"]["obligation"]["mailbox"].astext == mailbox,
        )
    ):
        state = row.payload.get("followup_v1", {})
        if state.get("obligation", {}).get("mailbox") == mailbox and state.get(
            "intent"
        ):
            intents.append(state["intent"])
    if any(
        i.get("state") not in {"crm_applied", "aborted_before_send"}
        or (i.get("state") == "crm_applied" and not i.get("sent_at"))
        for i in intents
    ):
        raise FollowupError("mailbox_outcome_unresolved")
    confirmed = [timestamp(i["sent_at"]) for i in intents if i.get("sent_at")]
    if confirmed and (now - max(confirmed)).total_seconds() < 600:
        raise SpacingPending(max(confirmed) + timedelta(seconds=600))
    # Historical slot/day keys stay readable for rollback, never as quotas.
    return slot_key, day_key


def execute(session, principal, work_id, body, provider, *, now=None):
    import os

    if os.environ.get("CRM_FOLLOWUP_EXECUTE_ENABLED") != "true":
        raise FollowupError("followup_execute_disabled", 503)
    if "followups:execute" not in principal.scopes:
        raise FollowupError("execute_scope_required", 403)
    now = now or datetime.now(UTC)
    with session.begin():
        work = locked_work(session, principal.workspace_id, work_id)
        if ledger_name(work) != "followup_v1":
            raise FollowupError("not_followup_send_work")
        state = deepcopy(work.payload["followup_v1"])
        mailbox_scope(principal, state["obligation"]["mailbox"])
        existing = state["intent"] is not None
        if not existing:
            raw, _reserved_review = approved_payload(
                session, principal, work, body, provider, now
            )
            try:
                slot, day = reserve_slot(session, work, now)
            except SpacingPending as exc:
                return defer_spacing(session, principal, work, exc.not_before)
            state["intent"] = {
                "state": "reserved",
                "intent_id": str(uuid4()),
                "slot_key": slot,
                "day_key": day,
                "reserved_at": utc(now),
                "payload_sha256": body.payload_sha256,
                "provider_message_id": None,
                "sent_at": None,
                "last_checked_at": None,
                "reason_code": "intent_committed",
            }
            save_ledger(work, state)
            audit(
                session,
                work,
                principal.actor_id,
                "intent_reserved",
                {"intent_id": state["intent"]["intent_id"], "slot_key": slot},
            )
        obligation_key_value = work.source_key
    # Successful return from COMMIT is the only crossing capability. Ambiguous
    # commit exceptions escape BEFORE send. No fresh process can recreate it.
    if existing:
        return recover(session, principal, work_id, obligation_key_value, provider)
    reservation_id = state["intent"]["intent_id"]
    post_attempted = False
    try:
        with session.begin():
            work = locked_work(session, principal.workspace_id, work_id)
            current_intent = work.payload["followup_v1"]["intent"]
            if (not current_intent or current_intent["state"] != "reserved"
                    or current_intent["intent_id"] != reservation_id):
                raise FollowupError("intent_requires_recovery")
            raw, _item = approved_payload(
                session, principal, work, body, provider, datetime.now(UTC)
            )
            thread_id = work.payload["followup_v1"]["obligation"]["thread_id"]
            # Keep CRM human-edit fences through the bounded provider POST.
            # The intent was committed separately, so rollback/crash here cannot
            # erase it. Gmail itself still has no cross-system transaction.
            if datetime.now(UTC) < datetime(2026, 10, 20, 7, 0, tzinfo=UTC):
                raise FollowupError("send_start_pending")
            post_attempted = True
            try:
                provider.send(raw, thread_id)
            except Exception:  # noqa: BLE001 -- ambiguous outcome never permits another POST
                state = deepcopy(work.payload["followup_v1"])
                state["intent"].update(
                    state="outcome_unknown", reason_code="provider_outcome_unknown"
                )
                save_ledger(work, state)
    except (FollowupError, WorkConflict, ValueError):
        if post_attempted:
            # A storage/finalization error after POST is recovery-only, never
            # proof that the provider did not accept the message.
            raise
        with session.begin():
            work = locked_work(session, principal.workspace_id, work_id)
            state = deepcopy(work.payload["followup_v1"])
            if (not state["intent"] or state["intent"]["state"] != "reserved"
                    or state["intent"]["intent_id"] != reservation_id):
                raise FollowupError("intent_requires_recovery") from None
            state["intent"].update(
                state="aborted_before_send", reason_code="fresh_revalidation_failed"
            )
            state["decision"] = None
            save_ledger(work, state)
        raise FollowupError("fresh_revalidation_failed") from None
    return recover(session, principal, work_id, obligation_key_value, provider)


def recover(session, principal, work_id, key, provider):
    from src.crm.connectors.followup_email import decode_payload, receipt_matches
    from src.crm.persistence.models import Activity, SourceIdentity

    with session.begin():
        work = locked_work(session, principal.workspace_id, work_id)
        if ledger_name(work) != "followup_v1" or work.source_key != key:
            raise FollowupError("recovery_identity_mismatch")
        state = deepcopy(work.payload["followup_v1"])
        obligation, intent = state["obligation"], state["intent"]
        mailbox_scope(principal, obligation["mailbox"])
        if intent is None:
            raise FollowupError("no_send_intent")
        if intent["state"] == "crm_applied":
            return work.result
        if intent["state"] == "aborted_before_send":
            return {
                "work_id": str(work.id),
                "intent_state": "aborted_before_send",
                "disposition": "deferred",
            }
        item = next(
            (
                r
                for r in state["review"]["requests"]
                if r["status"] == "approved"
                and r["package"].get("payload_sha256") == intent["payload_sha256"]
            ),
            None,
        )
        if item is None:
            raise FollowupError("intent_payload_unavailable")
        raw = decode_payload(item["package"]["payload_raw_b64"])
        _locked_company_lead(session, work.workspace_id, work.lead_id)
        _locked_rows(
            session,
            select(Task).where(
                Task.workspace_id == work.workspace_id, Task.id == work.task_id
            ),
        )
        context = current_context(
            session,
            principal,
            work.lead_id,
            obligation["mailbox"],
            provider,
            thread_ids=[obligation["thread_id"]],
        )
        matches = [
            m
            for m in messages(context)
            if receipt_matches(m, raw, obligation, timestamp(intent["reserved_at"]))
        ]
        intent["last_checked_at"] = utc(datetime.now(UTC))
        if len(matches) != 1:
            intent.update(
                state="outcome_unknown", reason_code="sent_not_uniquely_verified"
            )
            save_ledger(work, state)
            return {
                "work_id": str(work.id),
                "intent_state": "outcome_unknown",
                "disposition": "needs_context",
            }
        sent = matches[0]
        # Preserve manual edits. Provider SENT is historical fact, not authority
        # to overwrite a human Task/Lead/context change or mint another action.
        task = session.get(Task, work.task_id)
        unchanged = digest(context["crm"]) == digest(item["package"]["context"]["crm"])
        provider_before = deepcopy(context["provider"])
        for thread in provider_before["threads"]:
            thread["messages"] = [
                m for m in thread["messages"] if m["id"] != sent["id"]
            ]
        unchanged = unchanged and digest(provider_before) == digest(
            item["package"]["context"]["provider"]
        )
        observed = apply_observation(
            session,
            work.workspace_id,
            {
                "provider": "gmail",
                "source_scope": "mailbox:" + obligation["mailbox"],
                "id": sent["id"],
                "thread_id": sent["thread_id"],
                "occurred_at": sent["sent_at"],
                "direction": "outbound",
                "contact_emails": sent["to"] + sent["cc"],
                "subject": sent["headers"]["subject"],
                "excerpt": sent["plain"][:2000],
                "attachments": [],
            },
        )
        anchor_work = locked_work(session, work.workspace_id, UUID(observed["id"]))
        anchor_work.payload = deepcopy(anchor_work.payload) | {
            "followup_anchor_v1": {
                "schema": "ordinary-followup-anchor/v1",
                "purpose": "anchor_classification",
                "mailbox": obligation["mailbox"],
                "lead_id": str(work.lead_id),
                "thread_id": sent["thread_id"],
                "provider_message_id": sent["id"],
                "classification": f"fu{obligation['fu_stage']}",
                "decision": {
                    "purpose": "own_sent_receipt",
                    "source_work_id": str(work.id),
                    "intent_id": intent["intent_id"],
                    "observation_version": digest(sent),
                },
                "assessment": {
                    "disposition": "already_satisfied",
                    "reason_code": "own_sent_receipt",
                    "not_before": None,
                    "evidence_refs": [sent["id"]],
                },
                "last_evaluated_at": utc(datetime.now(UTC)),
                "review": {"current_request_id": None, "requests": []},
            }
        }
        anchor_work.status = "completed"
        anchor_work.last_lease_token = anchor_work.lease_token
        anchor_work.lease_token = anchor_work.lease_until = None
        activity = session.scalar(
            select(Activity)
            .join(SourceIdentity, Activity.source_identity_id == SourceIdentity.id)
            .where(
                Activity.workspace_id == work.workspace_id,
                SourceIdentity.workspace_id == work.workspace_id,
                SourceIdentity.source_system == "gmail",
                SourceIdentity.source_scope == "mailbox:" + obligation["mailbox"],
                SourceIdentity.entity_kind == "message",
                SourceIdentity.external_id == sent["id"],
                Activity.lead_id == work.lead_id,
            )
        )
        if activity is None:
            raise FollowupError("receipt_identity_unavailable")
        next_task = None
        if (
            unchanged
            and task.status == "open"
            and task.version == state["task_version"]
        ):
            task.status, task.completed_at, task.completion_activity_id = (
                "completed",
                timestamp(sent["sent_at"]),
                activity.id,
            )
            session.flush()
            if obligation["fu_stage"] < MAX_STAGE:
                next_task = create_obligation(
                    session,
                    work.workspace_id,
                    principal.actor_id,
                    context,
                    sent,
                    obligation["fu_stage"] + 1,
                )
        intent.update(
            state="crm_applied",
            provider_message_id=sent["id"],
            sent_at=sent["sent_at"],
            reason_code="receipt_applied" if unchanged else "receipt_only_human_change",
        )
        save_ledger(work, state)
        work.status = "completed"
        work.last_lease_token = work.lease_token
        work.lease_token = work.lease_until = None
        work.result = {
            "work_id": str(work.id),
            "intent_state": "crm_applied",
            "disposition": "already_satisfied",
            "activity_id": str(activity.id),
            "provider_message_id": sent["id"],
            "next_task_id": next_task["task_id"] if next_task else None,
            "reason_code": intent["reason_code"],
        }
        work.result_hash = digest(work.result)
        from src.crm.services.followup_projection import persist
        persist(session, anchor_work, context, sent, f"fu{obligation['fu_stage']}",
                {"disposition": "eligible" if unchanged else "deferred",
                 "reason_code": intent["reason_code"]})
        audit(
            session,
            work,
            principal.actor_id,
            "sent_receipt_applied",
            {"intent_id": intent["intent_id"], "activity_id": str(activity.id)},
        )
        session.flush()
        return work.result
