"""Full, versioned company evidence. No search floor or silent row truncation.

Private DTOs preserve every mapped source column (decimal values are strings),
including all-status tasks and original human notes. Hashes exclude capture time.
Reads are repeated across provider I/O: a cutoff alone is not a mutable snapshot.
"""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import inspect, or_, select

from src.crm.connectors.followup_email import read_context
from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    Lead,
    Meeting,
    Proposal,
    SourceIdentity,
    Task,
)


def canonical(value):
    def check(item):
        if item is None or type(item) in (str, int, bool):
            return
        if type(item) is list:
            for child in item:
                check(child)
        elif type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
        else:
            raise ValueError("noncanonical_context")

    check(value)
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive_source_time")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, (UUID, Decimal, date)):
        return str(value)
    if isinstance(value, list):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    return value


def dto(row):
    return {
        c.key: json_value(getattr(row, c.key)) for c in inspect(type(row)).column_attrs
    }


def rows(session, model, workspace, *filters):
    """Keyset all-page traversal; refresh identity-map rows on every read."""
    result, cursor = [], None
    # Company identity rows are small. Keep complete traversal but avoid sixty
    # network round trips for a ~2,000-company identity graph on each snapshot.
    page_size = 1000 if model in (Lead, Contact, Account) else 100
    while True:
        query = select(model).where(model.workspace_id == workspace, *filters)
        if cursor is not None:
            query = query.where(model.id > cursor)
        page = list(
            session.scalars(
                query.order_by(model.id)
                .limit(page_size)
                .execution_options(populate_existing=True)
            )
        )
        result.extend(page)
        if len(page) < page_size:
            return result
        cursor = page[-1].id


def crm_snapshot(session, workspace, lead_id):
    leads = rows(session, Lead, workspace)
    lead = next((r for r in leads if r.id == lead_id), None)
    if lead is None:
        raise ValueError("lead_unavailable")
    contacts = rows(session, Contact, workspace)
    accounts = rows(session, Account, workspace)
    selected = {lead.id}
    account_ids, names, emails = set(), set(), set()
    while True:
        before = (len(selected), len(account_ids), len(names), len(emails))
        for item in leads:
            if item.id in selected:
                if item.account_id:
                    account_ids.add(item.account_id)
                if item.company_name:
                    names.add(item.company_name.casefold().strip())
                if item.contact_email:
                    emails.add(str(item.contact_email))
        for account in accounts:
            if account.id in account_ids or account.normalized_name in names:
                account_ids.add(account.id)
                names.add(account.display_name.casefold().strip())
        for contact in contacts:
            if contact.account_id in account_ids and contact.primary_email:
                emails.add(str(contact.primary_email))
        for item in leads:
            if (
                item.account_id in account_ids
                or (item.company_name or "").casefold().strip() in names
                or item.contact_email in emails
            ):
                selected.add(item.id)
        if before == (len(selected), len(account_ids), len(names), len(emails)):
            break
    scope = lambda model: or_(
        model.lead_id.in_(selected), model.account_id.in_(account_ids)
    )
    activities = [dto(r) for r in rows(session, Activity, workspace, scope(Activity))]
    selected_leads = [dto(r) for r in leads if r.id in selected]
    source_ids={v.source_identity_id for v in leads if v.id in selected and v.source_identity_id}
    sources = rows(session, SourceIdentity, workspace, SourceIdentity.id.in_(source_ids)) if source_ids else []
    # Source metadata can contain older/corrected recipient identities. Preserve
    # it in the package; Gmail queries also include historical contact_emails.
    selected_sources = [
        dto(r)
        for r in sources
        if r.id in {v.source_identity_id for v in leads if v.id in selected}
    ]
    for source in selected_sources:
        metadata = source.get("metadata_json") or {}
        for key in ("email", "contact_email", "primary_email"):
            if isinstance(metadata.get(key), str) and "@" in metadata[key]:
                emails.add(metadata[key])
    # Reuse source-bound commercial review; never infer an email pause from a
    # callback date or introduce a second text classifier. Included in read-CAS.
    from src.crm.services.commercial_profile import read_profiles
    profiles = read_profiles(session, workspace, [r for r in leads if r.id in selected])
    crm = {
        "commercial_profiles": [profiles[key] for key in sorted(profiles, key=str)],
        "lead": dto(lead)
        | {"related_leads": selected_leads, "sources": selected_sources},
        "contacts": [dto(r) for r in contacts if r.account_id in account_ids],
        "account": (
            {
                "records": [dto(r) for r in accounts if r.id in account_ids],
                "meetings": [
                    dto(r) for r in rows(session, Meeting, workspace, scope(Meeting))
                ],
                "proposals": [
                    dto(r) for r in rows(session, Proposal, workspace, scope(Proposal))
                ],
            }
            if account_ids
            else None
        ),
        "notes": [a for a in activities if a["activity_type"] == "note"],
        "calls": [a for a in activities if a["activity_type"] == "call"],
        "tasks_all_statuses": [
            dto(r) for r in rows(session, Task, workspace, scope(Task))
        ],
        "activities": activities,
    }
    return crm, sorted(emails)


def page_records(records, binding, *, cursor=None, limit=50):
    """Actor/filter-bound snapshot cursor: mutations invalidate, never drop rows."""
    import base64
    import hmac
    import os
    from datetime import UTC, datetime, timedelta

    from src.crm.services.followup_review import (
        FollowupError,
        canonical,
        strict_json,
        timestamp,
        utc,
    )

    records = sorted(records, key=lambda item: item["id"])
    if len({item["id"] for item in records}) != len(records):
        raise FollowupError("duplicate_inventory_identity")
    secret = os.environ.get("CRM_AUTOMATION_BEARER_TOKEN", "")
    if len(secret) < 32:
        raise FollowupError("cursor_key_missing", 503)
    fingerprint = digest(records)
    now = datetime.now(UTC)
    state = {
        "binding": binding,
        "snapshot_hash": fingerprint,
        "after": "",
        "cutoff": utc(now),
        "expires_at": utc(now + timedelta(minutes=5)),
    }
    if cursor:
        try:
            if len(cursor) > 4096:
                raise ValueError
            value = strict_json(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)),
                maximum=4096,
            )
            supplied = value["body"]
            mac = hmac.new(
                secret.encode(), canonical(supplied), hashlib.sha256
            ).hexdigest()
            if (
                not hmac.compare_digest(mac, value["mac"])
                or supplied["binding"] != binding
                or supplied["snapshot_hash"] != fingerprint
                or timestamp(supplied["expires_at"]) <= now
            ):
                raise ValueError
            state = supplied
        except (KeyError, ValueError, TypeError):
            raise FollowupError("snapshot_cursor_invalid") from None
    rest = [item for item in records if item["id"] > state["after"]]
    items = rest[:limit]
    next_cursor = None
    if len(rest) > limit:
        state = state | {"after": items[-1]["id"]}
        mac = hmac.new(secret.encode(), canonical(state), hashlib.sha256).hexdigest()
        next_cursor = (
            base64.urlsafe_b64encode(canonical({"body": state, "mac": mac}))
            .decode()
            .rstrip("=")
        )
    response = {
        "items": items,
        "next_cursor": next_cursor,
        "complete": next_cursor is None,
        "total": len(records),
        "snapshot_hash": fingerprint,
        "cutoff": state["cutoff"],
    }
    if len(canonical(response)) > 1024 * 1024:
        raise FollowupError("page_payload_too_large")
    return response


def collect_context(session, workspace, lead_id, mailbox, provider, *, thread_ids=()):
    from src.crm.persistence.models import AgentWork

    crm, recipients = crm_snapshot(session, workspace, lead_id)
    related = {item["id"] for item in crm["lead"]["related_leads"]}
    def durable_threads():
        return sorted({w.payload["thread_id"] for w in rows(session, AgentWork, workspace,
                           AgentWork.lead_id.in_([UUID(value) for value in related]),
                           AgentWork.payload['provider'].astext=='gmail',
                           AgentWork.payload['source_scope'].astext=='mailbox:'+mailbox,
                           AgentWork.payload['thread_id'].astext.is_not(None))
                       if str(w.lead_id) in related and w.payload.get("provider") == "gmail"
                       and w.payload.get("source_scope") == "mailbox:" + mailbox
                       and w.payload.get("thread_id")})
    known_threads = durable_threads()
    gmail = read_context(provider, mailbox, recipients, thread_ids=set(thread_ids) | set(known_threads))
    current, current_recipients = crm_snapshot(session, workspace, lead_id)
    if (digest(crm) != digest(current) or recipients != current_recipients
            or known_threads != durable_threads()):
        raise ValueError("crm_context_changed")
    # read_context fences each complete read with the mailbox historyId. Between
    # reads, use the full company-scoped content: unrelated mailbox activity is
    # not a change to this review. Relevant messages, drafts, signature, contacts
    # and tasks are still fetched again and participate in these exact hashes.
    crm_hash, provider_hash = digest(crm), digest(gmail["provider"])
    context_hash = digest(
        {
            "crm": crm,
            "provider": gmail["provider"],
        }
    )
    return {
        "crm": crm,
        "provider": gmail["provider"],
        "recipients": recipients,
        "context_hash": context_hash,
        "source_versions": {
            "lead_version": crm["lead"]["version"],
            "crm_manifest_sha256": crm_hash,
            "provider_manifest_sha256": provider_hash,
        },
        "coverage": {
            "complete": True,
            "coverage_id": context_hash,
            "crm_complete": True,
            "gmail_complete": True,
            "direct_drafts_complete": True,
            "related_threads_complete": True,
        },
    }
