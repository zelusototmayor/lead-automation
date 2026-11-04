"""Source-bound reviewed next channel, using existing commercial-profile receipts.

Categorical interpretation belongs to Sales/owner review of full context. This
module only binds that interpretation; it never calls a model or sends anything.
"""
import hashlib
import json
from uuid import UUID
from sqlalchemy import select, or_
from src.crm.persistence.models import Activity, Lead, Task
from src.crm.services.note_source_service import source_digest


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def scope_ids(session, workspace_id, lead):
    """Same transitive account/company/phone equivalence as CALL selection."""
    return scope_ids_for_leads(session, workspace_id, [lead])


def scope_ids_for_leads(session, workspace_id, leads):
    """Union of scopes, loading and normalizing workspace identities once per call."""
    from src.crm.services.call_day import _phone, _company
    ids, keys = {lead.id for lead in leads}, set()
    if not ids:
        return []
    rows = list(session.scalars(select(Lead).where(Lead.workspace_id == workspace_id)))
    identities = [(row.id, {k for k in (
        ('account', row.account_id), ('company', _company(row.company_name)),
        ('phone', _phone(row.contact_phone))) if k[1]}) for row in rows]
    while True:
        before = (len(ids), len(keys))
        for row_id, aliases in identities:
            if row_id in ids or aliases & keys:
                ids.add(row_id); keys.update(aliases)
        if (len(ids), len(keys)) == before:
            return sorted(ids, key=str)


def context_sources(session, workspace_id, lead):
    ids = scope_ids(session, workspace_id, lead)
    rows = list(session.scalars(select(Activity).where(Activity.workspace_id == workspace_id,
        Activity.lead_id.in_(ids))))
    superseded = {a.supersedes_activity_id for a in rows if a.supersedes_activity_id}
    return [a for a in rows if a.id not in superseded and a.summary and
        (a.activity_type == 'email_received' or a.activity_type in ('note', 'call', 'meeting', 'proposal')
         and a.actor_type not in ('agent', 'automation', 'system', 'service'))]


def context_hash(sources):
    return digest(sorted((str(a.id), source_digest(a)) for a in sources))


def task_hash(session, workspace_id, lead):
    ids = scope_ids(session, workspace_id, lead)
    rows = list(session.scalars(select(Task).where(Task.workspace_id == workspace_id, Task.lead_id.in_(ids))))
    return digest(sorted((str(t.id), t.version, t.status, t.due_at, t.title, t.call_intent) for t in rows))


def validate_binding(session, workspace_id, lead, value, *, applying=False):
    sources = context_sources(session, workspace_id, lead)
    if value['context_sha256'] != context_hash(sources):
        return False
    by_id = {str(a.id): a for a in sources}
    for ref in value['source_refs']:
        source = by_id.get(str(ref['activity_id']))
        if source is None or ref['source_digest'] != source_digest(source):
            return False
    if applying and value['tasks_sha256'] != task_hash(session, workspace_id, lead):
        return False
    return bool(value['source_refs'])
