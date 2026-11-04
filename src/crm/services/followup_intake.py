"""Read-only Gmail-first inventory. Identity gaps are blockers, never no_sent.

No date floor, CRM anchor prerequisite, inferred commercial class or new leads.
A scan freezes the exact mailbox message IDs before hydration. Normal CRM
updates or later incoming mail cannot erase progress. Discovery is not send
permission: each reconcile and send still requires fresh per-lead context.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID, uuid4

from src.crm.connectors.followup_email import ProviderReadError, message_dto
from src.crm.persistence.models import AgentWork, Contact, Lead
from src.crm.services.followup_context import digest, dto, rows
from src.crm.services.followup_intake_checkpoint import load_refs, save_refs
from src.crm.services.followup_review import (
    FollowupError,
    canonical,
    strict_json,
    timestamp,
    utc,
)
from src.crm.services.followup_service import mailbox_scope


def inventory_digest(value):
    """Hash persisted JSON, including finite classifier probabilities.

    Inventory identity includes full AgentWork results. Unlike the strict
    review protocol, those source JSON columns legitimately contain floats.
    Preserve numeric types/values; never coerce, omit or permit NaN/Infinity.
    """
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def mark_ref(seen, message_id):
    if message_id in seen:
        raise ProviderReadError("provider_page_repeated_identity")
    seen.add(message_id)


def inventory_cursor(state, *, snapshot, binding):
    secret = os.environ.get("CRM_AUTOMATION_BEARER_TOKEN", "")
    if len(secret) < 32:
        raise FollowupError("cursor_key_missing", 503)
    if state is None:
        return {"snapshot_hash": snapshot, "binding": binding, "page_token": None,
                "count": 0, "scanned": 0, "profile_scanned": 0, "scan_id": uuid4().hex,
                "refs_hash": digest([]),
                "expires_at": utc(datetime.now(UTC) + timedelta(hours=24))}
    try:
        if len(state) > 4096:
            raise ValueError
        wrapped = strict_json(base64.urlsafe_b64decode(state + "=" * (-len(state) % 4)), maximum=4096)
        body = wrapped["body"]
        signature = hmac.new(secret.encode(), canonical(body), hashlib.sha256).hexdigest()
        if (not hmac.compare_digest(signature, wrapped["mac"])
                or body["snapshot_hash"] != snapshot or body["binding"] != binding
                or timestamp(body["expires_at"]) <= datetime.now(UTC)
                or type(body["page_token"]) is not str or not body["page_token"]
                or type(body["count"]) is not int or body["count"] < 0
                or type(body["scanned"]) is not int or body["scanned"] < body["count"]
                or type(body["profile_scanned"]) is not int
                or not 0 <= body["profile_scanned"] <= body["scanned"]
                or UUID(body["scan_id"]).hex != body["scan_id"]
                or ("history_id" in body and (type(body["history_id"]) is not str
                                             or not 1 <= len(body["history_id"]) <= 128))
                or type(body["refs_hash"]) is not str or len(body["refs_hash"]) != 64):
            raise ValueError
        return body
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise FollowupError("snapshot_cursor_invalid") from None


def sign_cursor(body):
    secret = os.environ["CRM_AUTOMATION_BEARER_TOKEN"]
    mac = hmac.new(secret.encode(), canonical(body), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(canonical({"body": body, "mac": mac})).decode().rstrip("=")


def sent_inventory(session, principal, mailbox, provider, *, cursor=None, limit=50):
    mailbox_scope(principal, mailbox)
    deadline = time.monotonic() + 45
    profile = provider.get("/profile")
    if profile.get("emailAddress") != mailbox or not profile.get("historyId"):
        raise ProviderReadError("mailbox_mismatch")
    if type(profile.get("messagesTotal")) is not int or profile["messagesTotal"] < 0:
        raise ProviderReadError("mailbox_total_unavailable")
    def identity_snapshot():
        snapshot = {model.__name__: [dto(r) for r in rows(session, model, principal.workspace_id)]
                    for model in (Lead, Contact)}
        # Only these rows can match `known` below; preserve ALL their columns,
        # kinds and statuses so conflicting identities remain explicit blockers.
        # No result/payload projection and no snapshot cache across requests.
        snapshot["AgentWork"] = [dto(r) for r in rows(
            session, AgentWork, principal.workspace_id,
            AgentWork.payload["source_scope"].astext == "mailbox:" + mailbox,
            AgentWork.payload["message_id"].astext.isnot(None),
        )]
        return snapshot
    before = identity_snapshot()
    binding = {"workspace": str(principal.workspace_id), "actor": str(principal.actor_id),
               "mailbox": mailbox, "kind": "followup_sent_inventory"}
    if cursor:
        # Authenticate before trusting any persisted manifest identity.
        try:
            if type(cursor) is not str or len(cursor) > 4096:
                raise ValueError
            wrapped = strict_json(base64.urlsafe_b64decode(
                cursor + "=" * (-len(cursor) % 4)), maximum=4096)
            state = inventory_cursor(cursor, snapshot=wrapped["body"]["snapshot_hash"], binding=binding)
            if (state.get("scan_kind") != "frozen_ids_v1"
                    or type(state.get("inventory_count")) is not int
                    or not state["scanned"] <= state["inventory_count"]
                    or str(state["scanned"]) != state["page_token"]
                    or type(state.get("inventory_hash")) is not str
                    or len(state["inventory_hash"]) != 64):
                raise ValueError
        except (ValueError, TypeError, KeyError, binascii.Error):
            raise FollowupError("snapshot_cursor_invalid") from None
        snapshot = state["snapshot_hash"]
        # Existing private cache verifies the entire immutable manifest, not a
        # mutable Gmail page offset. The cursor only advances hydration progress.
        all_refs = load_refs(state | {"scanned": state["inventory_count"],
                                     "refs_hash": state["inventory_hash"]})
    else:
        all_refs, seen_ids, seen_tokens = [], set(), set()
        page_token = None
        while True:
            if time.monotonic() >= deadline:
                raise ProviderReadError("provider_deadline")
            page = provider.get("/messages", {
                # Enumerate IDs only; avoid search-index lag and per-message RAW
                # latency while freezing the working set. MIME comes below.
                "maxResults": 500, "includeSpamTrash": "true",
                **({"pageToken": page_token} if page_token else {}),
            })
            refs = page.get("messages", [])
            if not isinstance(refs, list) or len(refs) > 500:
                raise ProviderReadError("provider_page_invalid")
            for ref in refs:
                if (not isinstance(ref, dict)
                        or any(type(ref.get(k)) is not str or not ref[k]
                               or ":" in ref[k] or len(ref[k]) > 120
                               for k in ("id", "threadId"))):
                    raise ProviderReadError("provider_page_invalid")
                mark_ref(seen_ids, ref["id"])
                all_refs.append(ref["id"] + ":" + ref["threadId"])
            page_token = page.get("nextPageToken")
            if page_token is None:
                break
            if (not isinstance(page_token, str) or not page_token
                    or page_token in seen_tokens):
                raise ProviderReadError("provider_cursor_repeated")
            seen_tokens.add(page_token)
        # Only ID enumeration requires a stable mailbox. New messages after
        # this short phase belong to the next daily scan, not an endless reset.
        if provider.get("/profile") != profile:
            raise ProviderReadError("intake_sources_changed")
        snapshot = inventory_digest({"binding": binding, "refs": all_refs,
                                     "history_id": profile["historyId"]})
        state = inventory_cursor(None, snapshot=snapshot, binding=binding)
        state.update(scan_kind="frozen_ids_v1", inventory_count=len(all_refs),
                     inventory_hash=digest(all_refs), observed_at=utc(datetime.now(UTC)))
        save_refs(state, all_refs)
    start = state["scanned"]
    frozen_page = all_refs[start:start + min(limit, 50)]
    refs = [dict(zip(("id", "threadId"), ref.split(":"))) for ref in frozen_page]
    profile_scanned = state["profile_scanned"]
    contacts = {c["id"]: c["primary_email"] for c in before["Contact"]}
    records = []
    for ref in refs:
        if time.monotonic() >= deadline:
            raise ProviderReadError("provider_deadline")
        if not isinstance(ref, dict) or not ref.get("id") or not ref.get("threadId"):
            raise ProviderReadError("provider_page_invalid")
        value = provider.get("/messages/" + quote(ref["id"], safe=""), {"format": "minimal"})
        if (not isinstance(value, dict) or value.get("id") != ref["id"]
                or value.get("threadId") != ref["threadId"]):
            raise ProviderReadError("sent_identity_changed")
        # Gmail can omit the empty label array on a full RAW message. Only
        # absence defaults to []; explicit null/bad types remain errors. No
        # sender, MIME, or thread evidence may substitute for an actual SENT.
        labels = value.get("labelIds", [])
        if (not isinstance(labels, list)
                or any(not isinstance(label, str) or not label for label in labels)
                or len(set(labels)) != len(labels)):
            raise ProviderReadError("provider_labels_invalid")
        # Gmail's profile total excludes SPAM/TRASH in the verified mailbox.
        # Still enumerate and deduplicate ALL refs, including SENT in Trash.
        if not {"SPAM", "TRASH"}.intersection(labels):
            profile_scanned += 1
        # Unrelated incoming MIME is not part of SENT discovery. Direct provider
        # identity/labels/count are mandatory for every ref; SENT MIME and all
        # relevant per-lead contexts retain their strict parser unchanged.
        if "SENT" not in labels:
            continue
        if "DRAFT" in labels:
            raise ProviderReadError("sent_identity_changed")
        # Only SENT needs MIME/attachments. Identity and labels remain verified
        # for every frozen ID; no change to snapshot coverage or SENT evidence.
        raw_value = provider.get("/messages/" + quote(ref["id"], safe=""), {"format": "raw"})
        if (raw_value.get("id") != ref["id"] or raw_value.get("threadId") != ref["threadId"]
                or sorted(raw_value.get("labelIds", [])) != sorted(labels)):
            raise ProviderReadError("sent_identity_changed")
        message = message_dto(raw_value)
        from src.crm.services.account_service import normalize_email
        recipients = {normalize_email(value) for value in message["to"] + message["cc"]}
        candidates = [lead for lead in before["Lead"] if recipients.intersection(
            {normalize_email(value) for value in (lead.get("contact_email"), contacts.get(lead.get("contact_id"))) if value})]
        lead = candidates[0] if len(candidates) == 1 else None
        known = [w for w in before["AgentWork"]
                 if w["payload"].get("source_scope") == "mailbox:" + mailbox
                 and w["payload"].get("message_id") == message["id"]]
        reason = ("recipient_identity_ambiguous" if len(candidates) > 1
                  else "recipient_identity_missing" if not lead else "anchor_unclassified")
        disposition = "needs_context" if not lead else "needs_anchor_review"
        if message["from"] != [mailbox] or not recipients:
            disposition, reason = "needs_context", "sent_sender_or_recipient_unverified"
        # Inventory is evidence, not permission: calls/meetings or commercial
        # stage must not hide SENT. The canonical fresh assessment/review keeps
        # closed/suppressed leads and proposals from autonomous sending.
        elif known and lead:
            if (len(known)==1 and known[0]['kind']=='unmatched_observation'
                    and known[0]['lead_id'] is None and known[0]['status']=='completed'):
                disposition,reason='needs_anchor_review','contact_now_resolved'
            elif (len(known) != 1 or known[0]["lead_id"] != lead["id"]
                    or known[0]["kind"] != "sent_review"):
                disposition, reason = "needs_context", "existing_observation_identity_conflict"
            elif "followup_anchor_v1" in known[0]["payload"]:
                disposition, reason = "existing_work", "canonical_observation_exists"
            # Legacy generic sent_review must enter v1.2 on the SAME work row;
            # it is not evidence of a classified anchor or an email completion.
        records.append({
            "id": message["id"], "thread_id": message["thread_id"],
            "sent_at": message["sent_at"], "lead_id": lead["id"] if lead else None,
            "disposition": disposition, "reason_code": reason,
            "observation_hash": digest(message),
        })
    total = state["count"] + len(records)
    scanned = state["scanned"] + len(refs)
    next_cursor = sign_cursor(state | {
        "page_token": str(scanned), "count": total, "scanned": scanned,
        "profile_scanned": profile_scanned, "refs_hash": digest(all_refs[:scanned]),
    }) if scanned < len(all_refs) else None
    return {
        "items": records, "next_cursor": next_cursor, "complete": next_cursor is None,
        "total": total, "snapshot_hash": snapshot,
        "gmail_audited": next_cursor is None, "send_authority": False,
        "observed_at": datetime.now(UTC).isoformat(),
    }
