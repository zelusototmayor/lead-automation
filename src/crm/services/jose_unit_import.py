"""Trusted unit import after frozen RAW/ledger validation; no public upload API.

Called only by the guarded migration runner. Timeline/unit evidence identifiers
stay original; canonical unit IDs bind the identical spans to the fresh source.
"""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid5
from sqlalchemy import select
from src.crm.persistence.models import Activity, AgentWork, Lead
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
from src.crm.services.note_source_service import source_digest
from src.crm.services.jose_note_sources import prepare_source
from src.crm.services import jose_note_policy_v2, jose_note_units
from src.crm.services.jev_note_worker import load_snapshot, _receipt, KIND
from src.crm.services.jev_note_runtime import target_identity
from src.crm.services.jev_note_policy import digest

VALID_STATUSES=frozenset(('structural','reused','reused_v2','delta_succeeded'))


def import_units(session,principal,*,entry,expected_lead_version,expected_source_digest):
    original=entry['source'];observed=original['source'];event_id=observed['activity_id']
    if original['workspace_id']!=str(principal.workspace_id):raise ValueError('offline_workspace_changed')
    expected=jose_note_policy_v2.source_units(original)
    if (not entry['units'] or any(u['status'] not in VALID_STATUSES for u in entry['units'])
            or [u['snapshot'] for u in entry['units']]!=expected):
        raise ValueError('offline_units_changed_or_incomplete')
    lead_id=UUID(original['lead_id'])
    if session.scalar(select(Lead.id).where(Lead.workspace_id==principal.workspace_id,Lead.id==lead_id)) is None:
        raise ValueError('offline_lead_changed')
    lead,_=_locked_company_lead(session,principal.workspace_id,lead_id)
    rows=_locked_rows(session,select(Activity).where(Activity.workspace_id==principal.workspace_id,Activity.lead_id==lead_id))
    source=next((r for r in rows if str(r.id)==event_id),None)
    if (source is None or lead.version!=expected_lead_version or source_digest(source)!=expected_source_digest
            or any(r.supersedes_activity_id==source.id for r in rows)
            or observed['summary']!=source.summary or observed['activity_type']!=source.activity_type
            or observed.get('outcome_code')!=source.outcome_code or observed.get('call_details')!=source.call_details
            or datetime.fromisoformat(observed['occurred_at'])!=source.occurred_at
            or observed['provenance'].get('actor_type')!=source.actor_type
            or observed['provenance'].get('title')!=source.title
            or (observed['provenance'].get('source_system') is not None
                and observed['provenance']['source_system']!=source.source_system)):
        raise ValueError('offline_source_changed')
    snapshot=load_snapshot(session,principal.workspace_id,lead_id,
        {'activity_id':event_id,'source_digest':expected_source_digest},flags=jose_note_units.FLAGS)
    request=jose_note_units.build_request(snapshot)
    canonical=jose_note_policy_v2.source_units(snapshot)
    if len(canonical)!=len(expected):raise ValueError('offline_unit_count_changed')
    units=[]
    for part,previous in zip(canonical,entry['units']):
        binding=part['source']['unit'];old=previous['snapshot']['source']['unit']
        if any(binding[k]!=old[k] for k in ('start','end','kind','date_literal','date_timezone')):
            raise ValueError('offline_unit_span_changed')
        units.append({'unit':binding,'evidence_unit':old,'classification':deepcopy(previous['decisions']),
            'status':previous['status'],'evidence_receipts':{k:previous[k] for k in
                ('reused_receipt_id','reused_delta_receipt_id','delta_receipt_id') if previous.get(k)}})
    target=target_identity(session)
    key='jev-personal-note:'+digest({'target':target,'event_id':event_id,'version':jose_note_units.VERSION})
    receipt_id=uuid5(principal.workspace_id,key)
    binding={'bundle_sha256':entry['bundle_sha256'],'entry_sha256':digest(entry),
        'timeline_source_digest':observed['source_digest'],'canonical_source_digest':expected_source_digest}
    prior=session.get(AgentWork,receipt_id)
    if prior is not None:
        if (prior.workspace_id!=principal.workspace_id or prior.kind!=KIND or prior.result_hash!=digest(prior.result)
                or prior.result.get('offline_import')!=binding or prior.result.get('decisions')!={'units':units}
                or prior.result.get('decision_id')!=request['decision_id']):
            raise ValueError('offline_import_collision')
        return {'receipt_id':str(prior.id),'work_id':prior.result['work_id'],'replayed':True}
    prepared=prepare_source(session,principal,lead_id,source.id,
        SimpleNamespace(expected_lead_version=expected_lead_version,source_digest=expected_source_digest))
    result=_receipt(request,simulated=False)
    result.update(receipt_id=str(receipt_id),work_id=prepared['work_id'],status='succeeded',target=target,
        decisions={'units':units},reserved_microeur=0,cost_status='offline_group_reserved_not_billed',offline_import=binding)
    row=AgentWork(id=receipt_id,workspace_id=principal.workspace_id,lead_id=lead_id,source_key=key,kind=KIND,
        status='completed',attempts=0,payload={k:request[k] for k in ('event_id','source_digest','snapshot_digest','flags')},
        result=result,result_hash=digest(result))
    session.add(row);session.flush()
    return {'receipt_id':str(row.id),'work_id':prepared['work_id'],'replayed':False}
