"""Evidence-backed contact detail; drafts and completed work are not sends."""
from datetime import UTC, datetime
from sqlalchemy import or_, select
from src.crm.activity_provenance import operational_activity_filter
from src.crm.persistence.models import Activity, AgentWork, SourceIdentity, Lead
from src.crm.services.note_source_service import source_digest


def commercial_context(session, workspace_id, lead_id):
    lead = session.scalar(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id == lead_id))
    rows = list(session.scalars(select(Activity).where(
        Activity.workspace_id == workspace_id, Activity.lead_id == lead_id,
        Activity.activity_type.in_(('call','email_sent')),
        or_(Activity.activity_type != 'call', operational_activity_filter())).order_by(
        Activity.occurred_at.desc(), Activity.id.desc())))
    email = {'delivery_status': 'unconfirmed', 'draft_exists': False,
             'draft_status': 'not_observed', 'sent_at': None, 'source_ref': None,
             'provenance': None,
             'contact_ref': {'lead_id':str(lead_id),
                             'contact_id':str(lead.contact_id) if lead and lead.contact_id else None}}
    for row in rows:
        if row.activity_type != 'email_sent' or row.direction != 'outbound':
            continue
        source = session.get(SourceIdentity, row.source_identity_id) if row.source_identity_id else None
        if row.actor_type == 'human':
            provenance = 'manual_record'
        elif (source and source.workspace_id == workspace_id and
              source.source_system == 'gmail' and source.entity_kind == 'message'):
            provenance = 'gmail_observation'
        else:
            continue
        email.update(delivery_status='sent', sent_at=row.occurred_at.isoformat(),
            provenance=provenance, source_ref={'activity_id':str(row.id),
                'provider_message_id':source.external_id if source else None})
        break
    work = list(session.scalars(select(AgentWork).where(AgentWork.workspace_id == workspace_id,
        AgentWork.lead_id == lead_id, AgentWork.kind == 'call_followup')
        .order_by(AgentWork.created_at.desc()).limit(50)))
    for item in work:
        for ref in (item.result or {}).get('tasks',[]):
            receipt = ref.get('draft') or {}
            if receipt.get('draft_id') and receipt.get('sent') is False and receipt.get('verified_by'):
                email.update(draft_exists=True, draft_status='verified_at_apply_not_current',
                    draft_ref={'work_id':str(item.id),'draft_id':receipt['draft_id']})
                break
    from src.crm.services.commercial_kpi_service import SourceIndex, effective
    index = SourceIndex(session, workspace_id)
    call = next((row for row in rows if row.activity_type == 'call'
                 and row.id not in index.exclusions and row.id not in index.conflicts), None)
    projection = next(((item.payload or {}).get('post_call') for item in work
        if call and (item.payload or {}).get('activity_id') == str(call.id)
            and (item.payload or {}).get('source_digest') == source_digest(call)), None)
    qualification = projection.get('qualification') if projection else None
    if call:
        current = effective(session, workspace_id, call.id, index=index)
        assessment = current.get('assessment') or {}
        relevance = {'value':assessment.get('relevant','unknown') if assessment.get('freshness') == 'current' else 'unknown',
                     'freshness':assessment.get('freshness','pending'),
                     'provenance':assessment.get('provenance'),
                     'evidence_refs':assessment.get('evidence_refs',[])}
    else:
        relevance = {'value':'unknown','freshness':'pending','evidence_refs':[]}
    return {'schema_version':1, 'qualification':{
        **(qualification or {'interest':{'value':'unknown'},'decision_role':{'value':'unknown'},
                             'result':{'value':call.outcome_code if call else 'unknown'}}),
        'relevance':relevance}, 'email':email,
        'post_call':{'status':'prepared' if projection else 'pending',
                    'source_activity_id':str(call.id) if call else None,
                    'short_note':(call.summary or '')[:280] if call else '',
                    'has_full_note':bool(call and len(call.summary or '')>280),
                    'sales_handoff':projection.get('sales_handoff') if projection else None},
        'generated_at':datetime.now(UTC).isoformat()}
