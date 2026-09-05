#!/usr/bin/env python3
"""Import one frozen workbook into a workspace, preserving source truth.

Row replay never overwrites human edits. Suppression is precomputed across the
whole archive (including quarantined rows), so duplicate-email order cannot revive
a refused contact. No import operation sends email or creates Calendar events.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import getaddresses
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from src.crm.persistence.models import Workspace, Account, Contact, Lead, Task, Activity, SourceIdentity, IngestEvent, Proposal, ProposalVersion
from src.crm.services.account_service import normalize_company_name, normalize_email
from src.crm.domain.stage_policy import stage_rank

STAGES = {'': 'new', 'new': 'new', 'no answer': 'contacted', 'call back': 'contacted', 'send email': 'contacted', 'email sent': 'contacted', 'contacted': 'contacted', 'warm': 'qualified', 'meeting booked': 'meeting_booked', 'proposal sent': 'proposal_sent', 'lost': 'lost', 'not a fit': 'not_a_fit', 'won': 'won'}
TERMINAL = {'lost', 'not_a_fit', 'won'}
PROPOSAL_SIGNALS = {'sent','follow-up 1','follow-up 2','follow-up 3','reactivation','lost','won','accepted','negotiation'}
REFUSAL = re.compile(r'\b(?:contra (?:a )?ia|nao (?:esta|estao) interessad[oa]s?|nao (?:tem|temos|têm) interesse|nao contactar|nao quer(?:em)? ser contactad[oa]s?|do not contact|unsubscribe|not interested)\b')


def folded(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(value).casefold()) if not unicodedata.combining(c))


def date_value(value, clock='09:00'):
    if not value: return None
    for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            day = datetime.strptime(str(value).strip(), fmt)
            parsed_time = datetime.strptime(clock or '09:00', '%H:%M').time()
            return datetime.combine(day.date(), parsed_time, ZoneInfo('Europe/Lisbon'))
        except ValueError: pass
    raise ValueError('invalid date')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def email_identities(value):
    result=set()
    for _,address in getaddresses([str(value or '')]):
        try:result.add(normalize_email(address))
        except ValueError:pass
    return sorted(result)


def suppression_reasons(row):
    reasons=[]
    for field, values in [('Stage',{'lost','not a fit'}),('Outcome',{'not a fit','lost'}),('Proposal Outcome',{'lost'}),('Proposal Status',{'lost'})]:
        if row.get(field,'').casefold() in values:reasons.append(field)
    if REFUSAL.search(folded(' '.join(row.get(k,'') for k in ('notes','What Happened','Outcome','Proposal Lost Reason')))):
        reasons.append('explicit_refusal_in_notes')
    return reasons


def proposal_evidence(row):
    """A legacy initial-email writer reused Proposal Sent; date alone proves none."""
    if row.get('Stage','').casefold()=='proposal sent':return 'explicit_proposal_stage'
    if row.get('Proposal Status','').casefold() in PROPOSAL_SIGNALS:return 'explicit_proposal_status'
    if row.get('Proposal Outcome','').casefold() in {'lost','won','accepted'}:return 'explicit_proposal_outcome'
    if row.get('Proposal Next Action') or row.get('Proposal Lost Reason'):return 'structured_proposal_context'
    notes=folded(' '.join(row.get(k,'') for k in ('notes','What Happened')))
    if re.search(r'\b(?:proposta enviada|enviei (?:a )?proposta|enviad[oa] (?:a )?proposta|proposal sent)\b',notes):return 'explicit_sent_proposal_note'
    return None


def import_workbook(workbook, session, workspace_id, owner_id):
    stamp = datetime.fromisoformat(workbook['captured_at'].replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('snapshot captured_at requires timezone')
    imported_at=datetime.now(timezone.utc)
    if stamp>imported_at:raise ValueError('snapshot captured_at is in the future')
    sheet = next(item for item in workbook['worksheets'] if item['name'] == 'PT Logistics')
    headers = sheet['values'][0]
    if len(headers)!=len(set(headers)) or 'Company' not in headers:raise ValueError('invalid or duplicate source headers')
    scope = workbook['spreadsheet_id'] + ':PT Logistics'
    archive_hash = digest(workbook)
    content_hash=digest({'spreadsheet_id':workbook['spreadsheet_id'],'worksheets':workbook['worksheets']})
    report = Counter();quarantine=[]
    # Serialize whole-workbook replay and prohibit silently importing changed
    # snapshots into a workspace whose human state may already have diverged.
    from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
    uow=SqlAlchemyUnitOfWork(lambda:session);uow.session=session;uow.lock_identities(workspace_id,('release-workbook:'+scope,))
    if session.get(Workspace, workspace_id) is None:
        session.add(Workspace(id=workspace_id, slug='sales-'+str(workspace_id)[:8], name='José · Comercial', timezone='Europe/Lisbon'));session.flush()
    archive_id=uuid5(workspace_id,scope+':release-archive')
    archive=session.get(SourceIdentity,archive_id)
    if archive and archive.metadata_json.get('snapshot_sha256')!=archive_hash:
        raise ValueError('different workbook snapshot requires a new/reconciled workspace')
    if not archive:
        session.add(SourceIdentity(id=archive_id,workspace_id=workspace_id,source_system='google_sheets',entity_kind='document',source_scope=scope,external_id='release-archive',metadata_json={'snapshot_sha256':archive_hash,'source_content_sha256':content_hash,'captured_at':stamp.isoformat(),'imported_at':imported_at.isoformat(),'date_semantics':'captured_at is snapshot observation, not contact/proposal creation','all_worksheet_names':[x['name'] for x in workbook['worksheets']]}));session.flush()
    rows=[];suppressed_emails=defaultdict(list)
    for locator,values in enumerate(sheet['values'][1:],2):
        row={h:str(v).strip() if v is not None else '' for h,v in zip(headers,values)}
        if not any(row.values()):continue
        rows.append((locator,row));reasons=suppression_reasons(row)
        if reasons:
            for email in email_identities(row.get('Email')):suppressed_emails[email].append({'row':locator,'reasons':reasons})
    for locator,row in rows:
        report['source_rows']+=1;key=f'{scope}:row:{locator}';lead_id=uuid5(workspace_id,key);identity_id=uuid5(workspace_id,key+':source')
        prior=session.get(SourceIdentity,identity_id)
        if prior:
            if prior.metadata_json.get('snapshot_sha256')!=archive_hash or prior.metadata_json.get('legacy_row')!=row:raise ValueError('row replay differs from frozen source')
            report['replay_noop']+=1;continue
        company=row.get('Company','');raw_stage=row.get('Stage','').casefold();stage=STAGES.get(raw_stage)
        identities=email_identities(row.get('Email'));email=identities[0] if len(identities)==1 else None
        own_reasons=suppression_reasons(row);inherited=[item for address in identities for item in suppressed_emails[address]]
        suppressed=bool(own_reasons or inherited)
        source_meta={'locator':locator,'row_number':locator,'snapshot_sha256':archive_hash,'legacy_row':row,'normalized_email':email,'email_identities':identities,'suppressed':suppressed,'suppression_reasons':own_reasons,'suppression_source_rows':inherited,'calendar_event_id':row.get('Calendar Event ID') or None,'captured_at':stamp.isoformat(),'imported_at':imported_at.isoformat(),'date_semantics':'snapshot observation; historical interaction dates unknown unless explicit','proposal_evidence':proposal_evidence(row),'proposal_currency_basis':'legacy EUR reporting default; source currency unverified','legacy_email_date_ambiguity':bool(row.get('Proposal Sent') and not proposal_evidence(row))}
        reason=None
        if not company:reason='missing_company'
        elif stage is None:reason='unknown_stage'
        else:
            try:norm=normalize_company_name(company)
            except ValueError:reason='invalid_company_identity'
        identity=SourceIdentity(id=identity_id,workspace_id=workspace_id,source_system='google_sheets',entity_kind='lead',source_scope=scope,external_id=f'row:{locator}',canonical_entity_type=None if reason else 'lead',canonical_entity_id=None if reason else lead_id,metadata_json=source_meta)
        session.add(identity);session.flush()
        event_id=uuid5(workspace_id,key+':ingest')
        session.add(IngestEvent(id=event_id,workspace_id=workspace_id,source_system='google_sheets',source_scope=scope,event_type='release.snapshot-row-observed',schema_version=1,idempotency_key=key,external_event_id=f'row:{locator}',occurred_at=stamp,payload=row,payload_hash=digest(row),processing_status='review' if reason else 'applied',applied_at=None if reason else imported_at))
        session.flush()
        if reason:
            quarantine.append({'row':locator,'reason':reason,'source':row});report['quarantined']+=1;continue
        if row.get('Email') and not email:report['email_needs_review']+=1
        # Refusal propagates by exact mailbox, without inventing a lost deal date.
        if suppressed and stage not in TERMINAL:stage='not_a_fit'
        elif row.get('Outcome','').casefold()=='not a fit':stage='not_a_fit'
        elif row.get('Proposal Outcome','').casefold()=='lost':stage='lost'
        phone=row.get('Phone') or None
        if phone and len(phone)>64:phone=None;report['phone_needs_review']+=1
        account_id=uuid5(workspace_id,'account:'+norm);account=session.get(Account,account_id)
        if account is None:
            account=Account(id=account_id,workspace_id=workspace_id,display_name=company,normalized_name=norm,city=row.get('City') or None,website_url=row.get('Website') or None,source_origin='legacy_sheet_archive',source_identity_id=identity_id,owner_id=owner_id,commercial_vertical='logistics',highest_stage_rank=stage_rank(stage),lifecycle_stage='lost' if suppressed else ('proposal' if stage=='proposal_sent' else ('meeting' if stage=='meeting_booked' else 'potential')))
            session.add(account);session.flush()
        duplicate_contact=session.scalar(select(Contact).where(Contact.workspace_id==workspace_id,Contact.primary_email==email)) if email else None
        if duplicate_contact:
            report['duplicate_email_preserved_in_archive']+=1
            if suppressed:duplicate_contact.status='inactive'
            email=None
        contact_id=uuid5(workspace_id,key+':contact')
        primary=not bool(session.scalar(select(Contact.id).where(Contact.workspace_id==workspace_id,Contact.account_id==account_id,Contact.is_primary.is_(True))))
        session.add(Contact(id=contact_id,workspace_id=workspace_id,account_id=account_id,full_name=row.get('Contact') or None,phone=phone,primary_email=email,is_primary=primary,status='inactive' if suppressed else 'active'));session.flush()
        session.add(Lead(id=lead_id,workspace_id=workspace_id,account_id=account_id,contact_id=contact_id,company_name=company,contact_name=row.get('Contact') or None,contact_email=email,contact_phone=phone,city=row.get('City') or None,source_stage_raw=row.get('Stage') or None,stage=stage,highest_stage_rank=stage_rank(stage),priority=(row.get('Priority') or 'medium').lower(),owner_id=owner_id,source_origin='legacy_sheet_archive',source_identity_id=identity_id,commercial_vertical='logistics',created_at=imported_at,updated_at=imported_at));session.flush()
        notes='\n\n'.join(f'{name}: {row[name]}' for name in ('notes','What Happened','Last Touch Type','Outcome','Proposal Next Action','Proposal Lost Reason') if row.get(name))
        if notes:
            session.add(Activity(id=uuid5(workspace_id,key+':note'),workspace_id=workspace_id,account_id=account_id,lead_id=lead_id,contact_id=contact_id,activity_type='note',occurred_at=stamp,title='Contexto preservado do CRM anterior',summary=notes+'\n\nData da captura do CRM anterior; a data da interação original é desconhecida.',source_system='google_sheets',source_identity_id=identity_id,ingest_event_id=event_id,actor_type='migration'))
        if suppressed:report['suppressed']+=1
        due=None
        if row.get('Due'):
            try:due=date_value(row['Due'],row.get('Due Time') or '09:00')
            except ValueError:report['due_needs_review']+=1
        if due and stage not in TERMINAL and not suppressed:
            session.add(Task(id=uuid5(workspace_id,key+':callback'),workspace_id=workspace_id,account_id=account_id,lead_id=lead_id,task_type='call',title='Voltar a ligar',due_at=due,owner_user_id=owner_id,source_rule='release:legacy_callback'));report['callbacks']+=1
        if raw_stage=='send email' and stage not in TERMINAL and not suppressed:
            session.add(Task(id=uuid5(workspace_id,key+':email'),workspace_id=workspace_id,account_id=account_id,lead_id=lead_id,task_type='email',title='Preparar email após chamada',due_at=max(stamp,due) if due else stamp,owner_user_id=owner_id,source_rule='release:legacy_initial_email'));report['email_tasks']+=1
        if row.get('Proposal Sent'):
            basis=proposal_evidence(row)
            if not basis:
                report['ambiguous_proposal_dates_archived']+=1
            elif row.get('Proposal Outcome','').casefold() in {'lost','won'} or row.get('Proposal Status','').casefold() in {'lost','won'} or stage in {'lost','won'}:
                report['closed_proposals_without_close_date_archived']+=1
                quarantine.append({'row':locator,'reason':'closed_proposal_missing_close_date','source':row})
            else:
                try:sent=date_value(row['Proposal Sent'])
                except ValueError:report['proposal_needs_review']+=1
                else:
                    if sent>stamp:
                        report['proposal_needs_review']+=1
                        quarantine.append({'row':locator,'reason':'proposal_sent_after_snapshot','source':row})
                    else:
                        proposal_id=uuid5(workspace_id,key+':proposal');version_id=uuid5(workspace_id,key+':proposal:v1')
                        proposal=Proposal(id=proposal_id,workspace_id=workspace_id,account_id=account_id,lead_id=lead_id,title='Proposta · '+company,status='withdrawn' if suppressed else 'sent',currency='EUR',sent_at=sent,sent_verification_state='legacy_unverified',value_state='missing',owner_user_id=owner_id,next_action=None if suppressed else row.get('Proposal Next Action') or None)
                        session.add(proposal);session.flush();session.add(ProposalVersion(id=version_id,proposal_id=proposal_id,version_number=1,status='sent',sent_at=sent));session.flush();proposal.selected_version_id=version_id;report['proposals']+=1
        report['leads_imported']+=1
    session.flush()
    return dict(report),quarantine


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workbook',required=True,type=Path);p.add_argument('--workspace-id',required=True,type=UUID);p.add_argument('--owner-id',required=True,type=UUID);p.add_argument('--report',required=True,type=Path);p.add_argument('--apply',action='store_true');args=p.parse_args()
    raw=args.workbook.read_bytes();workbook=json.loads(raw);engine=create_engine(os.environ['DATABASE_URL'])
    try:
        with Session(engine) as session:
            report,quarantine=import_workbook(workbook,session,args.workspace_id,args.owner_id)
            if args.apply:session.commit()
            else:session.rollback()
    finally:engine.dispose()
    os.umask(0o077)
    args.report.write_text(json.dumps({'applied':args.apply,'snapshot_sha256':digest(workbook),'workbook_file_sha256':hashlib.sha256(raw).hexdigest(),'source_content_sha256':digest({'spreadsheet_id':workbook['spreadsheet_id'],'worksheets':workbook['worksheets']}),'captured_at':workbook['captured_at'],'counts':report,'quarantine':quarantine},ensure_ascii=False,indent=2))
    print(json.dumps({'applied':args.apply,**report}))

if __name__=='__main__':main()
