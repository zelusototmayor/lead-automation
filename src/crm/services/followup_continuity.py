"""SENT continuity evidence, distinct from execution/send authority.

The index reads only canonical CRM observations. Fresh Gmail/source CAS and
independent review remain mandatory in the existing reconciliation/send path.
"""
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid5

from src.crm.persistence.models import Activity, AgentWork, EmailMessage, Lead, SourceIdentity, Task
from src.crm.services.followup_context import digest, page_records, rows
from src.crm.services.followup_policy import POLICY_ID, obligation_key, task_id
from src.crm.services.followup_review import audit, save_ledger, timestamp, utc


SCHEMA = "ordinary-followup-continuity/v1"


def reopen_unclassified(session, principal, work, context, anchor):
    """Only the untouched initial classification purpose, once, on fresh CAS."""
    state = deepcopy(work.payload.get("followup_anchor_v1") or {})
    review = state.get("review") or {}
    if (work.kind != "sent_review" or work.status != "completed"
            or work.lease_token is not None or work.lease_until is not None
            or not state or state.get("classification") is not None
            or state.get("decision") or state.get("intent") or state.get("obligation")
            or state.get("preparation_recovery") or state.get("classification_recovery")
            or review.get("current_request_id") or review.get("requests")
            or state.get("mailbox") != context["provider"]["mailbox"]
            or state.get("lead_id") != str(work.lead_id)
            or state.get("thread_id") != anchor["thread_id"]
            or state.get("provider_message_id") != anchor["id"]):
        return False
    proof = {"reason_code": "completed_initial_anchor_unclassified",
             "context_hash": context["context_hash"],
             "prior_attempts": work.attempts, "prior_error": work.error,
             "prior_result_hash": work.result_hash,
             "reopened_at": utc(datetime.now(UTC))}
    state["classification_recovery"] = proof
    save_ledger(work, state)
    # Prior result and complete audit history stay intact. This is a different
    # purpose of the same observation, never a retry of an attempted email.
    work.status, work.attempts = "queued", 0
    work.available_at = datetime.now(UTC)
    audit(session, work, principal.actor_id, "initial_anchor_reopened", proof)
    return True


def task_proof(session, workspace, obligation):
    if not obligation or not obligation.get("task_id") or not obligation.get("work_id"):
        return None
    task = session.get(Task, UUID(obligation["task_id"]))
    due_work = session.get(AgentWork, UUID(obligation["work_id"]))
    state = (due_work.payload.get("followup_v1") or {}) if due_work else {}
    if (task is None or due_work is None or task.workspace_id != workspace
            or due_work.workspace_id != workspace or due_work.task_id != task.id
            or due_work.lead_id != task.lead_id or due_work.kind != "followup_due"
            or task.task_type != "email" or task.source_rule != POLICY_ID
            or task.status != "open" or task.version != state.get("task_version")):
        return None
    return {"id": str(task.id), "work_id": str(due_work.id), "status": task.status,
            "version": task.version, "due_at": utc(task.due_at),
            "source_rule": task.source_rule}


def receipt_continuity(session, work, source, lead):
    """Read back the send path's existing SENT/Activity/successor transaction.

    This is receipt coverage, never a fresh Gmail assessment or send permission.
    A human-changed receipt or absent/modified successor remains unresolved.
    """
    state = work.payload.get("followup_anchor_v1") or {}
    decision, klass = state.get("decision") or {}, state.get("classification")
    if (decision.get("purpose") != "own_sent_receipt" or klass not in {"fu1", "fu2", "fu3", "fu4", "fu5"}
            or not decision.get("source_work_id") or not decision.get("observation_version")
            or not source.get("activity_id") or lead is None
            or state.get("lead_id") != str(work.lead_id)
            or state.get("mailbox") != work.payload.get("source_scope", "")[8:]
            or source.get("lead_id") != str(work.lead_id)):
        return None
    original = session.get(AgentWork, UUID(decision["source_work_id"]))
    saved = (original.payload.get("followup_v1") or {}) if original else {}
    intent, result, obligation = saved.get("intent") or {}, (original.result or {}) if original else {}, saved.get("obligation") or {}
    if (original is None or original.kind != "followup_due" or original.status != "completed"
            or original.workspace_id != work.workspace_id
            or original.lead_id != work.lead_id or intent.get("state") != "crm_applied"
            or result.get("intent_state") != "crm_applied" or result.get("reason_code") != "receipt_applied"
            or not intent.get("sent_at") or intent.get("intent_id") != decision.get("intent_id")
            or intent.get("provider_message_id") != state.get("provider_message_id")
            or result.get("provider_message_id") != state.get("provider_message_id")
            or result.get("activity_id") != source["activity_id"]
            or obligation.get("mailbox") != state.get("mailbox")
            or obligation.get("thread_id") != state.get("thread_id")
            or obligation.get("fu_stage") != int(klass[-1])
            or not (saved.get("decision") or {}).get("context_hash")
            or not (saved.get("decision") or {}).get("reviewer_receipt_id")
            or timestamp(intent["sent_at"]) > datetime.now(UTC)):
        return None
    proof = None
    if klass != "fu5":
        key = obligation_key(work.workspace_id, state["mailbox"], work.lead_id,
                             state["thread_id"], int(klass[-1]) + 1, state["provider_message_id"])
        successor = {"task_id": str(task_id(work.workspace_id, key)),
                     "work_id": str(uuid5(work.workspace_id, "agent-work:" + key))}
        if result.get("next_task_id") != successor["task_id"]:
            return None
        proof = task_proof(session, work.workspace_id, successor)
        if not proof:
            return None
    return {"schema": SCHEMA, "state": "terminal" if klass == "fu5" else "next_step", "closed": True,
        "reason_code": "anchor_fu5" if klass == "fu5" else "canonical_next_task",
        "work_id": str(work.id), "mailbox": state["mailbox"], "lead_id": str(work.lead_id),
        "provider_message_id": state["provider_message_id"], "thread_id": state["thread_id"],
        "classification": klass, "context_hash": (saved.get("decision") or {}).get("context_hash"),
        "checked_at": intent["sent_at"], "lead_version": lead.version,
        "decision_hash": digest(decision), "owner_id": str(getattr(lead, "owner_id", None) or ""),
        "next_task": proof, "hold_until": None, "evidence_refs": [source["activity_id"], state["provider_message_id"]],
        "evidence_scope": "canonical_sent_receipt_and_open_task", "send_authority": False}


def consolidate(session, work, context, anchor, fresh_assessment, *, owner_id=None, now=None):
    """Persist a source-bound outcome; awaiting classification is never closed."""
    now = now or datetime.now(UTC)
    state = deepcopy(work.payload["followup_anchor_v1"])
    decision, klass = state.get("decision") or {}, state.get("classification")
    value = {"schema": SCHEMA, "state": "awaiting_review", "closed": False,
             "reason_code": "anchor_unclassified", "work_id": str(work.id),
             "mailbox": context["provider"]["mailbox"],
             "lead_id": str(work.lead_id), "thread_id": anchor["thread_id"],
             "provider_message_id": anchor["id"], "classification": klass,
             "context_hash": context["context_hash"],
             "coverage_id": context["coverage"]["coverage_id"],
             "lead_version": context["crm"]["lead"]["version"],
             "owner_id": str(work.payload.get("owner_id") or context["crm"]["lead"].get("owner_id") or owner_id or ""),
             "checked_at": utc(now), "next_task": None, "hold_until": None,
             "evidence_refs": [anchor["id"]], "revisit": "fresh_context_reconcile"}
    value["decision_hash"] = digest(decision)
    reviewed = (decision.get("purpose") == "anchor_classification"
                and decision.get("reviewer_receipt_id")
                and decision.get("observation_version") == digest(anchor))
    if klass and klass != "uncertain" and reviewed:
        reason = fresh_assessment["reason_code"]
        if klass in {"fu5", "non_sequence"} or fresh_assessment["disposition"] in {"suppressed", "sequence_finished"}:
            value.update(state="terminal", closed=True,
                         reason_code="anchor_" + klass if klass in {"fu5", "non_sequence"} else reason)
        elif fresh_assessment["disposition"] == "eligible":
            proof = task_proof(session, work.workspace_id, state.get("obligation"))
            value.update(state="next_step" if proof else "needs_reconciliation",
                         closed=proof is not None, next_task=proof,
                         reason_code="canonical_next_task" if proof else "next_task_missing_or_changed")
        else:
            until = fresh_assessment.get("not_before")
            refs = fresh_assessment.get("evidence_refs") or ["crm:" + digest(context["crm"])]
            explicit = reason in {"human_or_other_obligation", "explicit_wait_or_contact_review",
                                  "human_reply_contextual_action", "existing_direct_draft"}
            dated = bool(until and timestamp(until) > now and value["owner_id"])
            value.update(state="explicit_hold" if explicit else "needs_reconciliation",
                         closed=explicit and dated, reason_code=reason,
                         hold_until=until, evidence_refs=refs)
    elif decision.get("purpose") == "own_sent_receipt":
        # Own receipt is evidence of a send, not authority to fabricate a new
        # task after human changes. Its existing result must name the successor.
        source = session.get(AgentWork, UUID(decision["source_work_id"]))
        source_state = (source.payload.get("followup_v1") or {}) if source else {}
        intent = source_state.get("intent") or {}
        valid = (source is not None and source.workspace_id == work.workspace_id
                 and intent.get("state") == "crm_applied"
                 and intent.get("intent_id") == decision.get("intent_id")
                 and intent.get("provider_message_id") == anchor["id"]
                 and decision.get("observation_version") == digest(anchor))
        if valid and klass == "fu5":
            value.update(state="terminal", closed=True, reason_code="anchor_fu5")
        elif valid:
            if not state.get("obligation") and klass in {"fu1", "fu2", "fu3", "fu4"}:
                key = obligation_key(work.workspace_id, context["provider"]["mailbox"],
                    work.lead_id, anchor["thread_id"], int(klass[-1]) + 1, anchor["id"])
                successor = {"task_id": str(task_id(work.workspace_id, key)),
                             "work_id": str(uuid5(work.workspace_id, "agent-work:" + key)),
                             "obligation_key": key}
                if ((source.result or {}).get("next_task_id") == successor["task_id"]
                        and task_proof(session, work.workspace_id, successor)):
                    state["obligation"] = successor
            proof = task_proof(session, work.workspace_id, state.get("obligation"))
            if proof:
                from src.crm.services.followup_service import assessment
                fresh_assessment = assessment(context, anchor, own_task_id=proof["id"])
            eligible = fresh_assessment["disposition"] == "eligible"
            value.update(state="next_step" if proof and eligible else "terminal"
                         if fresh_assessment["disposition"] == "suppressed" else "needs_reconciliation",
                         closed=bool(proof and eligible) or fresh_assessment["disposition"] == "suppressed",
                         next_task=proof,
                         reason_code="canonical_next_task" if proof and eligible else
                         fresh_assessment["reason_code"] if not eligible else "own_receipt_next_task_unresolved")
    state["continuity"] = value
    save_ledger(work, state)
    return value


def inventory(session, principal, mailbox, *, cursor=None, limit=50):
    """All captured Gmail SENT observations, including unmapped source gaps."""
    from src.crm.services.followup_service import mailbox_scope
    mailbox_scope(principal, mailbox)
    scope, workspace = "mailbox:" + mailbox, principal.workspace_id
    sources = rows(session, SourceIdentity, workspace, SourceIdentity.source_system == "gmail",
                   SourceIdentity.source_scope == scope)
    identities = {s.id: s for s in sources}
    mailbox_ids = [s.id for s in sources if s.entity_kind == "mailbox"]
    email_rows = rows(session, EmailMessage, workspace, EmailMessage.direction == "outbound",
                      EmailMessage.mailbox_identity_id.in_(mailbox_ids)) if mailbox_ids else []
    activities = rows(session, Activity, workspace, Activity.source_system == "gmail",
                      Activity.direction == "outbound")
    by_message = {}
    for activity in activities:
        source = identities.get(activity.source_identity_id)
        if source and source.entity_kind == "message":
            by_message[source.external_id] = {"lead_id": str(activity.lead_id) if activity.lead_id else None,
                "activity_id": str(activity.id), "sent_at": utc(activity.occurred_at)}
    for email in email_rows:
        by_message.setdefault(email.provider_message_id, {"lead_id": None, "activity_id": None})
        by_message[email.provider_message_id].update(thread_id=email.provider_thread_id,
                                                    sent_at=utc(email.sent_at), email_message_id=str(email.id))
    works = rows(session, AgentWork, workspace,
                 AgentWork.payload["source_scope"].astext == scope,
                 AgentWork.payload["message_id"].astext.is_not(None))
    leads = {str(lead.id): lead for lead in rows(session, Lead, workspace)}
    # One provider identity can have auxiliary email_update work as well as
    # its semantic sent_review. Auxiliary UUID order must not choose the anchor.
    grouped = {}
    for work in works:
        grouped.setdefault(work.payload["message_id"], []).append(work)
    def priority(work):
        return (0 if work.kind == "sent_review" and work.payload.get("followup_anchor_v1")
                else 1 if work.kind == "sent_review" else 2 if work.kind == "proposal_review"
                else 3, str(work.id))
    works = [min(group, key=priority) for group in grouped.values()]
    records, seen = [], set()
    for work in works:
        payload, anchor = work.payload, work.payload.get("followup_anchor_v1") or {}
        original = payload.get("original_observation") or {}
        message_id = payload["message_id"]
        sent = (payload.get("direction") == "outbound" or original.get("direction") == "outbound"
                or work.kind in {"sent_review", "proposal_review"} or message_id in by_message)
        unknown = work.kind in {"unmatched_observation", "identity_review", "bulk_observation"} and not payload.get("direction")
        if not sent and not unknown:
            continue
        seen.add(message_id)
        source = by_message.get(message_id, {})
        lead_id = str(work.lead_id) if work.lead_id else source.get("lead_id")
        lead = leads.get(lead_id)
        value = deepcopy(anchor.get("continuity")) or receipt_continuity(session, work, source, lead) or {"schema": SCHEMA, "closed": False,
            "state": "awaiting_review" if lead_id and not anchor.get("classification") else "needs_reconciliation",
            "reason_code": "anchor_unclassified" if lead_id else "mapping_required",
            "classification": anchor.get("classification"), "checked_at": None}
        value.update(work_id=str(work.id), lead_id=lead_id, mailbox=mailbox,
                     provider_message_id=message_id,
                     thread_id=anchor.get("thread_id") or payload.get("thread_id") or source.get("thread_id"))
        if value.get("closed") and (lead is None or value.get("lead_version") != lead.version
                or value.get("decision_hash") != digest(anchor.get("decision") or {})):
            value.update(state="needs_reconciliation", closed=False, reason_code="crm_context_changed")
        if value.get("closed") and value.get("state") == "explicit_hold" and (
                not value.get("hold_until") or timestamp(value["hold_until"]) <= datetime.now(UTC)):
            value.update(state="needs_reconciliation", closed=False, reason_code="hold_expired")
        if not lead_id:
            value.update(state="mapping_gap" if sent else "source_unknown", closed=False,
                         reason_code="mapping_required" if sent else "direction_unknown")
        next_task = value.get("next_task")
        if value.get("state") == "next_step" and task_proof(session, workspace,
                {"task_id": next_task.get("id"), "work_id": next_task.get("work_id")} if next_task else {}) != next_task:
            value.update(state="needs_reconciliation", closed=False, reason_code="next_task_changed")
        if value.get("closed") and not source.get("activity_id"):
            value.update(state="needs_reconciliation", closed=False, reason_code="sent_activity_missing")
        records.append({"id": str(work.id), "work_id": str(work.id), "kind": work.kind,
            "work_status": work.status, "lead_id": lead_id, "mailbox": mailbox,
            "provider_message_id": message_id, "message_id": message_id,
            "thread_id": anchor.get("thread_id") or payload.get("thread_id") or source.get("thread_id"),
            "sent_at": source.get("sent_at") or payload.get("occurred_at"),
            "sent_observed": sent, "activity_id": source.get("activity_id"),
            "email_message_id": source.get("email_message_id"), "continuity": value,
            "observation_work_ids": sorted(str(row.id) for row in grouped[message_id])})
    for message_id, source in by_message.items():
        if message_id not in seen:
            records.append({"id": "source:" + message_id, "work_id": None, "kind": "source_gap",
                "work_status": None, "lead_id": source.get("lead_id"), "mailbox": mailbox,
                "provider_message_id": message_id, "message_id": message_id, "thread_id": source.get("thread_id"),
                "sent_at": source.get("sent_at"), "sent_observed": True,
                "activity_id": source.get("activity_id"), "email_message_id": source.get("email_message_id"),
                "continuity": {"schema": SCHEMA, "state": "needs_reconciliation", "closed": False,
                               "reason_code": "sent_review_missing", "checked_at": None}})
    latest_thread, latest_lead = {}, {}
    for row in records:
        if not row["sent_observed"]:
            continue
        order = (timestamp(row["sent_at"]) if row["sent_at"] else datetime.min.replace(tzinfo=UTC), row["id"])
        for mapping, key in ((latest_thread, row["thread_id"]), (latest_lead, row["lead_id"])):
            if key and (key not in mapping or order > mapping[key][0]):
                mapping[key] = (order, row["id"])
    for row in records:
        row["is_latest_thread"] = bool(row["thread_id"] and latest_thread.get(row["thread_id"], (None, None))[1] == row["id"])
        row["is_latest_lead"] = bool(row["lead_id"] and latest_lead.get(row["lead_id"], (None, None))[1] == row["id"])
    return page_records(records, {"workspace": str(workspace), "actor": str(principal.actor_id),
        "mailbox": mailbox, "kind": "followup_continuity"}, cursor=cursor, limit=limit) | {
        "schema": SCHEMA, "gmail_audited": False, "send_authority": False}
