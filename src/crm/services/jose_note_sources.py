"""Complete source inventory and non-actionable historical work anchors."""
from datetime import UTC
from uuid import uuid5
from sqlalchemy import select, func
from src.crm.persistence.models import Activity, AgentWork
from src.crm.services.agent_work_service import WorkConflict
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
from src.crm.services.note_source_service import source_digest, source_snapshot


def source_corpus(session, workspace_id, *, cutoff, cursor=None, limit=100):
    query=select(Activity).where(Activity.workspace_id==workspace_id,Activity.created_at<=cutoff)
    total=session.scalar(select(func.count()).select_from(query.subquery()))
    if cursor: query=query.where(Activity.id>cursor)
    rows=list(session.scalars(query.order_by(Activity.id).limit(limit+1)))
    ids=[row.id for row in rows[:limit]]
    superseded=set(session.scalars(select(Activity.supersedes_activity_id).where(
        Activity.workspace_id==workspace_id,Activity.supersedes_activity_id.in_(ids))))
    work={}
    for row in session.scalars(select(AgentWork).where(AgentWork.workspace_id==workspace_id,
            AgentWork.kind=='call_followup',AgentWork.payload['activity_id'].astext.in_([str(i) for i in ids]))
            .order_by(AgentWork.created_at,AgentWork.id)):
        work.setdefault((row.payload['activity_id'],row.payload.get('source_digest')),str(row.id))
    items=[]
    for row in rows[:limit]:
        signature=source_digest(row)
        items.append({**source_snapshot(row),'lead_id':str(row.lead_id) if row.lead_id else None,
            'account_id':str(row.account_id) if row.account_id else None,'source_digest':signature,
            'source_status':'superseded' if row.id in superseded else 'current',
            'work_id':work.get((str(row.id),signature)),
            'provenance':{'actor_type':row.actor_type,'source_system':row.source_system,'title':row.title,
                'actor_id':str(row.actor_id) if row.actor_id else None,
                'source_identity_id':str(row.source_identity_id) if row.source_identity_id else None,
                'ingest_event_id':str(row.ingest_event_id) if row.ingest_event_id else None}})
    return {'items':items,'total':total,'next_cursor':str(rows[limit-1].id) if len(rows)>limit else None,
        'cutoff':cutoff.astimezone(UTC).isoformat(),'scope':'all_workspace_events_no_origin_filter'}


def prepare_source(session, principal, lead_id, activity_id, body):
    """Never enqueue actionable work or resurrect a completed obligation."""
    lead,_=_locked_company_lead(session,principal.workspace_id,lead_id)
    if lead.version!=body.expected_lead_version: raise WorkConflict('Lead changed')
    sources=_locked_rows(session,select(Activity).where(Activity.workspace_id==principal.workspace_id,
        Activity.lead_id==lead_id))
    source=next((row for row in sources if row.id==activity_id),None)
    if (source is None or source.activity_type not in ('call','note') or not source.summary
            or source_digest(source)!=body.source_digest
            or any(row.supersedes_activity_id==source.id for row in sources)):
        raise WorkConflict('Current nonempty note required')
    existing=session.scalar(select(AgentWork).where(AgentWork.workspace_id==principal.workspace_id,
        AgentWork.kind=='call_followup',AgentWork.lead_id==lead_id,
        AgentWork.payload['activity_id'].astext==str(source.id),
        AgentWork.payload['source_digest'].astext==body.source_digest)
        .order_by(AgentWork.created_at,AgentWork.id).limit(1))
    if existing: return {'work_id':str(existing.id),'replayed':True,'source_work_unchanged':True}
    key='personal-note-anchor:'+str(source.id)+':'+body.source_digest
    work=AgentWork(id=uuid5(principal.workspace_id,key),workspace_id=principal.workspace_id,
        lead_id=lead_id,source_key=key,kind='call_followup',status='completed',attempts=0,
        payload={'activity_id':str(source.id),'source_digest':body.source_digest,'classification_only':True},
        result={'disposition':'classification_only_no_obligations'})
    session.add(work);session.flush()
    return {'work_id':str(work.id),'replayed':False,'source_work_unchanged':True}
