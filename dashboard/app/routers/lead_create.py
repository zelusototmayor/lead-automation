"""Small authenticated command for adding a company and calling contact."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from typing import Annotated
from uuid import UUID, uuid5
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select
from dashboard.app.db import create_database_engine, create_session_factory
from dashboard.app.feature_flags import require_postgres_command_writer
from dashboard.app.security import CRMPrincipal, require_crm_principal, _require_command_permission
from src.crm.persistence.models import Account, Contact, Lead, Activity, AuditEvent, SourceIdentity
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.ingestion.outbox import enqueue_outbox_event
from src.crm.services.account_service import normalize_company_name, normalize_email

router = APIRouter()

class CreateLead(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: UUID
    company: str = Field(min_length=1, max_length=512)
    contact_name: str | None = Field(default=None,max_length=512)
    email: str | None = Field(default=None,max_length=320)
    phone: str | None = Field(default=None,max_length=64)
    city: str | None = Field(default=None,max_length=255)
    notes: str | None = Field(default=None,max_length=10000)

    @field_validator('company','contact_name','email','phone','city','notes')
    @classmethod
    def clean(cls,value,info):
        if value is None:return value
        value=value.strip()
        if not value:
            if info.field_name=='company':raise ValueError('company required')
            return None
        if any(ord(c)<32 and c not in '\n\t' for c in value):raise ValueError('invalid text')
        if info.field_name=='email':return normalize_email(value)
        if info.field_name=='company':normalize_company_name(value)
        if info.field_name!='notes' and any(c in value for c in '\n\t'):raise ValueError('single-line identity required')
        return value

async def access(request:Request,principal:Annotated[CRMPrincipal,Depends(require_crm_principal)]):
    result=await _require_command_permission(request,principal,'crm:lead:create')
    require_postgres_command_writer()
    return result

@router.post('/api/v1/commands/leads')
def create_lead(body:CreateLead,principal:Annotated[CRMPrincipal,Depends(access)],idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key')]=None):
    if idempotency_key != str(body.command_id):raise HTTPException(409,'Command conflict')
    semantic=hashlib.sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    engine=create_database_engine()
    try:
        with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
            result=apply_create(uow,body,principal,semantic)
            uow.commit()
            return result
    finally:engine.dispose()

def apply_create(uow,body,principal,semantic):
    workspace=principal.workspace_id
    uow.lock_identities(workspace,(f'human-command:{body.command_id}',))
    prior=uow.outbox_events.by_command(workspace,body.command_id)
    if prior is not None:
        audit=uow.audit_events.by_command(workspace,body.command_id)
        if prior.semantic_hash!=semantic or audit is None or audit.actor_id!=principal.actor_id:raise HTTPException(409,'Command conflict')
        return {'command_id':str(body.command_id),'lead_id':str(prior.aggregate_id),'version':int(prior.payload['version']),'replayed':True}
    if body.email:
        uow.lock_identities(workspace,('email:'+body.email.casefold(),))
        protected=uow.session.scalar(select(SourceIdentity.id).where(
            SourceIdentity.workspace_id==workspace,
            SourceIdentity.metadata_json['suppressed'].as_boolean().is_(True),
            or_(SourceIdentity.metadata_json['normalized_email'].as_string()==body.email,
                SourceIdentity.metadata_json['email_identities'].contains([body.email]))))
        if protected:raise HTTPException(409,'Contact has protected history; review required')
        if uow.session.scalar(select(Contact.id).where(Contact.workspace_id==workspace,Contact.primary_email==body.email)) or uow.session.scalar(select(Lead.id).where(Lead.workspace_id==workspace,Lead.contact_email==body.email)):
            raise HTTPException(409,'Contact already exists')
    normalized=normalize_company_name(body.company)
    uow.lock_identities(workspace,('company:'+normalized,))
    accounts=list(uow.session.scalars(select(Account).where(Account.workspace_id==workspace,Account.normalized_name==normalized,Account.merged_into_account_id.is_(None))))
    if len(accounts)>1:raise HTTPException(409,'Company needs review')
    if accounts:
        protected=uow.session.scalar(select(SourceIdentity.id).join(Lead,Lead.source_identity_id==SourceIdentity.id).where(
            Lead.workspace_id==workspace,Lead.account_id==accounts[0].id,
            SourceIdentity.workspace_id==workspace,
            SourceIdentity.metadata_json['suppressed'].as_boolean().is_(True)))
        if protected:raise HTTPException(409,'Company has protected contact history; review required')
    account=accounts[0] if accounts else Account(id=uuid5(workspace,str(body.command_id)+':account'),workspace_id=workspace,display_name=body.company,normalized_name=normalized,city=body.city,owner_id=principal.actor_id,source_origin='human')
    if not accounts:uow.session.add(account);uow.session.flush()
    contact=Contact(id=uuid5(workspace,str(body.command_id)+':contact'),workspace_id=workspace,account_id=account.id,full_name=body.contact_name,primary_email=body.email,phone=body.phone,is_primary=not bool(uow.session.scalar(select(Contact.id).where(Contact.workspace_id==workspace,Contact.account_id==account.id,Contact.is_primary.is_(True)))))
    uow.session.add(contact);uow.session.flush()
    lead=Lead(id=uuid5(workspace,str(body.command_id)+':lead'),workspace_id=workspace,account_id=account.id,contact_id=contact.id,company_name=body.company,contact_name=body.contact_name,contact_email=body.email,contact_phone=body.phone,city=body.city,stage='new',highest_stage_rank=10,owner_id=principal.actor_id,source_origin='human')
    uow.session.add(lead);uow.session.flush()
    uow.session.add(Activity(id=uuid5(workspace,str(body.command_id)+':activity'),workspace_id=workspace,account_id=account.id,lead_id=lead.id,contact_id=contact.id,activity_type='note',occurred_at=datetime.now(timezone.utc),title='Contacto criado',summary=body.notes,source_system='manual',actor_type='human',actor_id=principal.actor_id))
    enqueue_outbox_event(uow,workspace_id=workspace,command_id=body.command_id,semantic_hash=semantic,event_type='lead.created',aggregate_type='lead',aggregate_id=lead.id,payload={'lead_id':str(lead.id),'version':lead.version})
    uow.audit_events.add(AuditEvent(id=uuid5(workspace,str(body.command_id)+':audit'),workspace_id=workspace,command_id=body.command_id,actor_id=principal.actor_id,action='lead.created',entity_type='lead',entity_id=lead.id,details={'version':lead.version}))
    return {'command_id':str(body.command_id),'lead_id':str(lead.id),'version':lead.version,'replayed':False}
