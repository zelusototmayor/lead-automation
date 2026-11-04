"""Canonical account activity command with provenance and optimistic concurrency."""
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid5
import hashlib,json
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from dashboard.app.routers.accounts import AccountRequestContext,get_account_request_context
from dashboard.app.db import create_database_engine,create_session_factory
from dashboard.app.feature_flags import require_postgres_command_writer
from dashboard.app.security import CRMPrincipal,require_crm_principal,_require_command_permission
from src.crm.persistence.models import Account,SourceIdentity,AuditEvent
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.ingestion.outbox import enqueue_outbox_event
from src.crm.services.company_types import COMPANY_TYPES,SCOPE,type_label,canonical_type,DETAILED_TYPES
router=APIRouter()
class CompanyTypeCommand(BaseModel):
    model_config=ConfigDict(extra='forbid')
    command_id:UUID
    expected_version:int=Field(ge=1)
    primary:str
    detailed_types:list[str]=Field(default_factory=list,max_length=8)
    secondary:list[str]=Field(default_factory=list,max_length=5)
    confidence:Literal['high','medium','low','unknown']
    status:Literal['provisional','review','verified']
    method:Literal['jev','source_evidence','human']
    sources:list[str]=Field(min_length=1,max_length=12)
    evidence:str=Field(min_length=1,max_length=12000)
    provider_receipt:str|None=Field(default=None,max_length=255)
    authority_ref:str=Field(min_length=1,max_length=512)
    @model_validator(mode='before')
    @classmethod
    def normalize_types(cls, value):
        value=dict(value)
        original=[value.get('primary'),*value.get('secondary',[])]
        details=list(value.get('detailed_types',[]))
        details.extend(x for x in original if x in DETAILED_TYPES and x!='review' and canonical_type(x)!=x)
        value['detailed_types']=list(dict.fromkeys(details))
        value['primary']=canonical_type(value.get('primary'))
        value['secondary']=list(dict.fromkeys(canonical_type(x) for x in value.get('secondary',[]) if canonical_type(x)!=value['primary']))
        return value
    @model_validator(mode='after')
    def validate_values(self):
        if any(x not in DETAILED_TYPES or x=='review' for x in self.detailed_types):raise ValueError('Unknown detailed activity')
        if self.primary not in COMPANY_TYPES or any(x not in COMPANY_TYPES or x=='review' for x in self.secondary):raise ValueError('Unknown business type')
        if len(set(self.secondary))!=len(self.secondary) or self.primary in self.secondary:raise ValueError('Duplicate business type')
        if any(not s.strip() or len(s)>2048 for s in self.sources):raise ValueError('Invalid source')
        if self.primary=='review' and self.status!='review':raise ValueError('Uncertain activity requires review')
        if self.method=='jev' and not self.provider_receipt:raise ValueError('Provider receipt required')
        if self.status=='verified' and self.method!='human':raise ValueError('Only human verification is final')
        return self

def identity_query(workspace,account_id):
    return select(SourceIdentity).where(SourceIdentity.workspace_id==workspace,SourceIdentity.source_system=='agent',SourceIdentity.entity_kind=='account',SourceIdentity.source_scope==SCOPE,SourceIdentity.external_id==str(account_id))

@router.get('/api/v1/company-types')
def taxonomy(context:Annotated[AccountRequestContext,Depends(get_account_request_context)]):
    return {'items':[{'code':k,**v} for k,v in COMPANY_TYPES.items()]}

@router.get('/api/v1/accounts/{account_id}/company-type')
def read_type(account_id:UUID,context:Annotated[AccountRequestContext,Depends(get_account_request_context)]):
    a=context.session.scalar(select(Account).where(Account.id==account_id,Account.workspace_id==context.principal.workspace_id))
    if a is None:raise HTTPException(404,'Account not found')
    e=context.session.scalar(identity_query(context.principal.workspace_id,account_id))
    return {'account_id':str(a.id),'version':a.version,'primary':canonical_type(a.sector),'label':type_label(a.sector),'evidence':e.metadata_json if e else None}

async def access(request:Request,principal:Annotated[CRMPrincipal,Depends(require_crm_principal)]):
    result=await _require_command_permission(request,principal,'crm:lead:edit')
    require_postgres_command_writer()
    return result

@router.post('/api/v1/commands/accounts/{account_id}/company-type')
def write_type(account_id:UUID,body:CompanyTypeCommand,principal:Annotated[CRMPrincipal,Depends(access)],idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key')]=None):
    if idempotency_key!=str(body.command_id):raise HTTPException(409,'Command conflict')
    semantic=hashlib.sha256(json.dumps({'account_id':str(account_id),**body.model_dump(mode='json')},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    engine=create_database_engine()
    try:
        with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
            workspace=principal.workspace_id
            uow.lock_identities(workspace,(f'human-command:{body.command_id}',f'company-type:{account_id}'))
            prior=uow.outbox_events.by_command(workspace,body.command_id)
            if prior is not None:
                audit=uow.audit_events.by_command(workspace,body.command_id)
                if prior.semantic_hash!=semantic or audit is None or audit.actor_id!=principal.actor_id:raise HTTPException(409,'Command conflict')
                return dict(prior.payload,replayed=True)
            a=uow.session.scalar(select(Account).where(Account.id==account_id,Account.workspace_id==workspace).with_for_update())
            if a is None:raise HTTPException(404,'Account not found')
            if a.version!=body.expected_version or a.merged_into_account_id is not None:raise HTTPException(409,'Account changed; reread required')
            e=uow.session.scalar(identity_query(workspace,account_id))
            before=e.metadata_json if e else None
            if body.method!='human' and (a.sector and (not before or before.get('status')=='verified' or before.get('method')=='human' or a.sector!=before.get('primary'))):raise HTTPException(409,'Preserve existing human classification')
            now=datetime.now(timezone.utc)
            metadata=body.model_dump(mode='json',exclude={'expected_version'})|{'updated_at':now.isoformat(),'label':type_label(body.primary)}
            if before and not metadata['detailed_types']:
                metadata['detailed_types']=before.get('detailed_types',[])
                if before.get('detailed_primary'):metadata['detailed_primary']=before['detailed_primary']
                if 'detailed_secondary' in before:metadata['detailed_secondary']=before['detailed_secondary']
            if e is None:
                e=SourceIdentity(id=uuid5(workspace,SCOPE+':'+str(account_id)),workspace_id=workspace,source_system='agent',entity_kind='account',source_scope=SCOPE,external_id=str(account_id),canonical_entity_type='account',canonical_entity_id=account_id,metadata_json=metadata)
                uow.session.add(e)
            else:e.metadata_json=metadata;e.last_seen_at=now
            # Full evidence belongs in immutable command snapshots; AuditEvent has
            # a 4096-byte DB bound. Keep full before/after history by reference.
            def history(value):
                if value is None:return None
                raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
                digest=hashlib.sha256(raw.encode()).hexdigest()
                hid=uuid5(workspace,SCOPE+':history:'+str(account_id)+':'+digest)
                prior_history=uow.session.get(SourceIdentity,hid)
                if prior_history is None:
                    uow.session.add(SourceIdentity(id=hid,workspace_id=workspace,source_system='agent',entity_kind='account',source_scope=SCOPE+'/history',external_id=str(account_id)+':'+digest,canonical_entity_type='account',canonical_entity_id=account_id,metadata_json=value))
                elif prior_history.metadata_json!=value:raise HTTPException(409,'Evidence history conflict')
                return {'id':str(hid),'sha256':digest}
            before_ref=history(before);after_ref=history(metadata)
            old_sector=a.sector;a.sector=body.primary;a.updated_at=now
            uow.session.flush()
            receipt={'command_id':str(body.command_id),'account_id':str(account_id),'version':a.version,'primary':a.sector,'replayed':False}
            enqueue_outbox_event(uow,workspace_id=workspace,command_id=body.command_id,semantic_hash=semantic,event_type='account.company_type_updated',aggregate_type='account',aggregate_id=account_id,payload=receipt)
            uow.audit_events.add(AuditEvent(id=uuid5(workspace,str(body.command_id)+':audit'),workspace_id=workspace,command_id=body.command_id,actor_id=principal.actor_id,action='account.company_type_updated',entity_type='account',entity_id=account_id,details={'before':{'primary':old_sector,'evidence_snapshot':before_ref},'after':{'primary':a.sector,'evidence_snapshot':after_ref},'version':a.version}))
            uow.commit()
            return receipt
    finally:engine.dispose()
