"""Persist a concise display note without changing immutable Sales sources."""
import hashlib
from uuid import uuid5
from sqlalchemy import select
from src.crm.persistence.models import SourceIdentity
SCOPE='import-note-brief/v1'
def validate_brief(text):
    if not isinstance(text,str):raise ValueError('Invalid note brief')
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if len(lines)>3 or len(text.split())>40 or any(not line.startswith('• ') for line in lines):raise ValueError('Use up to three short bullets (40 words)')
    if any(x in text.lower() for x in ['http://','https://','apollo','null']):raise ValueError('Evidence belongs outside the visible note')
    return '\n'.join(lines)
def store_brief(session,workspace,activity_id,original,brief,command_id,extra=None):
    brief=validate_brief(brief)
    key=uuid5(workspace,SCOPE+':'+str(activity_id))
    metadata={'activity_id':str(activity_id),'original_summary':original,'original_sha256':hashlib.sha256((original or '').encode()).hexdigest(),'summary':brief,'creation_command_id':str(command_id),**(extra or {})}
    prior=session.get(SourceIdentity,key)
    if prior:
        if prior.metadata_json!=metadata:raise ValueError('Existing brief differs; review current version')
        return prior
    row=SourceIdentity(id=key,workspace_id=workspace,source_system='agent',entity_kind='document',source_scope=SCOPE,external_id=str(activity_id),canonical_entity_type='activity',canonical_entity_id=activity_id,metadata_json=metadata)
    session.add(row)
    return row
def read_briefs(session,workspace,rows):
    sources={str(row.id):row.summary for row in rows if row.activity_type=='note'}
    if not sources:return {}
    entries=session.scalars(select(SourceIdentity).where(SourceIdentity.workspace_id==workspace,SourceIdentity.source_system=='agent',SourceIdentity.source_scope==SCOPE,SourceIdentity.entity_kind=='document',SourceIdentity.external_id.in_(sources)))
    # A later human edit must never be hidden behind an old generated summary.
    return {e.external_id:e.metadata_json['summary'] for e in entries if e.metadata_json.get('original_sha256')==hashlib.sha256((sources[e.external_id] or '').encode()).hexdigest()}
