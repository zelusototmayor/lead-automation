"""Trusted, classification-only v19 import. Not an HTTP answer-upload surface.

The operator freezes and verifies original Luna receipts before invoking this module.
The database transaction rechecks the complete source context, source digest,
lead revision and previous interpretation under locks; every correction is an
append-only audit, with a before-value kept in the immutable prior audit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from uuid import UUID, uuid5

from sqlalchemy import or_, select
from src.crm.persistence.models import Account, Activity, AgentWork, AuditEvent, Contact, Lead
from src.crm.services.jev_note_policy import digest
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
from src.crm.services.note_source_service import source_digest
from src.crm.services.jose_note_service import ACTION, WITHDRAW, current_audit

VERSION = 'historical-v19-embedded-date/live-patch4'
KIND = 'v19_classification_receipt'
OWNER_EVIDENCE_VERSION = 'owner-call-evidence/2026-09-30-v1'


def owner_evidence(entry,current):
    """Additive raw-model evidence, verified against frozen receipts by caller."""
    version=entry.get('owner_call_evidence_version')
    if version is None:
        if 'owner_call_evidence' in entry:
            raise ValueError('v19_owner_evidence_version')
        return None
    if (version!=OWNER_EVIDENCE_VERSION or current.get('actor_type')!='human' or
            not (current['activity_type']=='call' or
                current['activity_type']=='note' and current.get('title')=='Note added')):
        raise ValueError('v19_owner_evidence_scope')
    evidence=entry.get('owner_call_evidence'); choice=entry.get('prediction')
    if not isinstance(evidence,dict) or set(evidence)!={'kind','quote'} or not isinstance(choice,dict):
        raise ValueError('v19_owner_evidence_schema')
    kind=evidence['kind'];quote=evidence['quote']
    if kind is None:
        structured_no=(current.get('outcome_code') in ('no_answer','voicemail') or
            (current.get('call_details') or {}).get('answer_kind') in ('no_answer','voicemail','ivr_only'))
        if quote is not None or (choice.get('answered')=='no' and not structured_no):
            raise ValueError('v19_owner_negative_requires_quote')
    elif (kind not in ('no_answer','not_a_call') or not isinstance(quote,str) or not quote.strip()
            or quote not in (current.get('summary') or '') or choice.get('answered')!='no'
            or choice.get('meeting')!='no'):
        raise ValueError('v19_owner_negative_requires_bound_quote')
    return deepcopy(evidence)


def event_order(event):
    return (event.get('occurred_at') or '', event.get('created_at') or event.get('recorded_at') or '', str(event['id']))


def source_row(session, workspace_id, lead_id, *, lock=False):
    """Use the same complete account-linked context as the local Luna adapter."""
    lead, _ = _locked_company_lead(session, workspace_id, lead_id) if lock else (
        session.scalar(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id == lead_id)), None)
    if lead is None:
        raise ValueError('v19_lead_missing')
    def related(model, identifier):
        if not identifier:
            return None
        query = select(model).where(model.workspace_id == workspace_id, model.id == identifier)
        rows = _locked_rows(session, query) if lock else list(session.scalars(query))
        if len(rows) != 1:
            raise ValueError('v19_context_missing')
        return rows[0]
    # Account is already locked by _locked_company_lead. Lock the linked contact
    # before hashing its title/name; otherwise an edit can race between CAS and
    # audit insertion without changing the lead version.
    account = related(Account, lead.account_id)
    contact = related(Contact, lead.contact_id)
    scope = or_(Activity.lead_id == lead.id, Activity.account_id == lead.account_id) if lead.account_id else Activity.lead_id == lead.id
    query = select(Activity).where(Activity.workspace_id == workspace_id, scope)
    activities = _locked_rows(session, query) if lock else list(session.scalars(query))
    def iso(value):
        return value.isoformat() if value else None
    def event(row):
        return {'id':str(row.id),'activity_type':row.activity_type,'occurred_at':iso(row.occurred_at),
            'created_at':iso(row.created_at),'recorded_at':iso(row.recorded_at),
            'summary':row.summary,'actor_type':row.actor_type,'title':row.title,
            'outcome_code':row.outcome_code,'call_details':row.call_details,
            'supersedes_activity_id':str(row.supersedes_activity_id) if row.supersedes_activity_id else None,
            'lead_id':str(row.lead_id) if row.lead_id else None,'source_digest':source_digest(row)}
    return {'lead':{'id':str(lead.id),'company_name':lead.company_name,
             'contact_name':lead.contact_name,'contact_email':str(lead.contact_email) if lead.contact_email else None,
             'contact_phone':lead.contact_phone},
        'company':{'name':account.display_name} if account else None,
        'contact':{'full_name':contact.full_name,'title':contact.title,'phone':contact.phone} if contact else None,
        'activities':sorted((event(a) for a in activities), key=event_order),
        'lead_version':lead.version}


def source_hash(row, event_id):
    """The v19 adapter binds the current event and strictly prior context."""
    current, = (e for e in row['activities'] if e['id'] == event_id)
    previous = [e for e in row['activities'] if event_order(e) < event_order(current)]
    payload = {'lead_id':row['lead']['id'],'event':current,'prior':previous}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def context_hash(row):
    context = {'company':row.get('company') or row['lead'].get('company_name'),
        'contact_directory':{key:row['lead'].get(key) for key in
            ('contact_name','contact_email','contact_phone')},
        'linked_contact':row.get('contact')}
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


SEMANTIC_CONTEXT_VERSION = 'call-company-person/2026-10-08-v1'


def semantic_context_hash(row):
    """Call facts depend on company/person evidence, not delivery coordinates."""
    linked = row.get('contact') or {}
    company = row.get('company')
    names = sorted({name for name in (row['lead'].get('contact_name'),
        linked.get('full_name')) if name})
    value = {'version': SEMANTIC_CONTEXT_VERSION,
        'company': company.get('name') if isinstance(company, dict) else
            company or row['lead'].get('company_name'),
        'person_names': names, 'person_title': linked.get('title')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def receipt_context_current(session, source, receipt, context):
    """Legacy hashes require exact reconstruction, never a guessed migration.

    Keep the current company and linked person/title. Only delivery coordinates
    and duplicate legacy-name placement may differ. A matching original hash
    proves that no other classification context changed.
    """
    result = receipt.result
    semantic = result.get('semantic_context_sha256')
    if semantic is not None:
        return (result.get('semantic_context_version') == SEMANTIC_CONTEXT_VERSION
            and semantic == semantic_context_hash(context))
    expected = result.get('context_sha256')
    if expected == context_hash(context):
        return True
    lead = session.get(Lead, source.lead_id)
    if lead is None or lead.workspace_id != source.workspace_id:
        return False
    contact = session.get(Contact, lead.contact_id) if lead.contact_id else None
    if contact is not None and contact.workspace_id != source.workspace_id:
        return False
    emails = {None, context['lead'].get('contact_email')}
    phones = {None, context['lead'].get('contact_phone'),
        (context.get('contact') or {}).get('phone')}
    if contact is not None:
        emails.add(str(contact.primary_email) if contact.primary_email else None)
    ids = [lead.id] + ([contact.id] if contact is not None else [])
    changes = session.scalars(select(AuditEvent).where(
        AuditEvent.workspace_id == source.workspace_id, AuditEvent.entity_id.in_(ids),
        AuditEvent.created_at >= receipt.created_at,
        AuditEvent.action.in_(('contact.email_corrected', 'agent.contact_from_note'))
    ).order_by(AuditEvent.created_at.desc()).limit(40))
    for change in changes:
        details = change.details or {}
        for key in ('previous_email', 'confirmed_email'):
            if isinstance(details.get(key), str): emails.add(details[key])
        for side in ('before', 'after'):
            value = details.get(side) or {}
            if not isinstance(value, dict): continue
            for key in ('email', 'primary_email'):
                if isinstance(value.get(key), str): emails.add(value[key])
            if isinstance(value.get('phone'), str): phones.add(value['phone'])
    # Bound legacy compatibility work and keep unknown histories stale.
    if len(emails) > 8 or len(phones) > 8:
        return False
    from itertools import product
    current_semantic = semantic_context_hash(context)
    names = {None, context['lead'].get('contact_name'),
        (context.get('contact') or {}).get('full_name')}
    for name, email, phone, linked_phone in product(names, emails, phones, phones):
        candidate = dict(context, lead=dict(context['lead']))
        if context.get('contact') is not None:
            candidate['contact'] = dict(context['contact'])
        candidate['lead'].update(contact_name=name, contact_email=email, contact_phone=phone)
        if candidate.get('contact') is not None:
            candidate['contact']['phone'] = linked_phone
        if (semantic_context_hash(candidate) == current_semantic
                and context_hash(candidate) == expected):
            return True
    return False


def projection(entry, current):
    """Map five categorical dimensions without threshold/role masking."""
    choice = entry['prediction']
    evidence=owner_evidence(entry,current)
    if choice is None:
        if current['activity_type'] != 'note' or entry['source_unit'] is not None:
            raise ValueError('v19_noncall_invalid')
        return {'units': []}
    if (not isinstance(choice, dict) or set(choice) != {'answered','role','relevant','followup','meeting'}
            or choice['answered'] not in ('yes','no')
            or choice['role'] not in ('gatekeeper','decision_maker','not_applicable')
            or choice['relevant'] not in ('yes','no')
            or choice['followup'] not in ('yes','no')
            or choice['meeting'] not in ('agreed','no')
            or (choice['answered'] == 'no') != (choice['role'] == 'not_applicable')
            or (choice['relevant'] == 'yes') != (choice['role'] == 'decision_maker')):
        raise ValueError('v19_choices_invalid')
    unit = entry['source_unit']
    text = current.get('summary') or ''
    if (not isinstance(unit, dict) or unit.get('source_id') != current['id']
            or type(unit.get('start')) is not int or type(unit.get('end')) is not int
            or not 0 <= unit['start'] <= unit['end'] <= len(text)
            or text[unit['start']:unit['end']] != unit.get('text')
            or hashlib.sha256(unit['text'].encode()).hexdigest() != unit.get('source_sha256')
            or ((current['activity_type'] == 'call' or evidence is not None)
                and (unit['start'],unit['end']) != (0,len(text)))):
        raise ValueError('v19_source_span_invalid')
    binding = {'parent_activity_id':current['id'], 'start':unit['start'],'end':unit['end'],
        'kind':unit['kind'], 'date_literal':unit.get('date')}
    value = {'units':[{'unit':binding,'classification':{
        'call':{'value':True},'answered':{'value':choice['answered']=='yes'},
        'role':{'value':choice['role']},'relevant':{'value':choice['relevant']=='yes'},
        'followup':{'value':choice['followup']},
        'meeting':{'value':'yes' if choice['meeting']=='agreed' else 'no'},
        'callback':{'value':'unknown'}}}], 'prediction':deepcopy(choice)}
    if evidence is not None:
        value['owner_call_evidence_version']=OWNER_EVIDENCE_VERSION
        value['owner_call_evidence']=evidence
    return value


def apply(session, principal, *, event_id, expected_lead_version, expected_source_digest,
          expected_context_sha256, expected_audit_id, entry, verified_receipts_sha256):
    """One transaction, locks and CAS. Caller verifies RAW model receipts first."""
    workspace_id = principal.workspace_id
    lead_id = UUID(entry['lead_id']); event_id = UUID(str(event_id))
    row = source_row(session, workspace_id, lead_id, lock=True)
    matches = [e for e in row['activities'] if e['id'] == str(event_id)]
    if len(matches) != 1:
        raise ValueError('v19_source_missing_or_ambiguous')
    current = matches[0]
    if (row['lead_version'] != expected_lead_version or current['lead_id'] != str(lead_id)
            or current['source_digest'] != expected_source_digest
            or current['activity_type'] not in ('call','note')
            or any(a['supersedes_activity_id'] == str(event_id) for a in row['activities'])
            or entry['version'] != VERSION or entry['model'] != 'gpt-6-luna'
            or entry['event_id'] != str(event_id)
            or entry['source_sha256'] != source_hash(row, str(event_id))
            or expected_context_sha256 != context_hash(row)
            or entry['receipt_sha256'] != verified_receipts_sha256
            or not isinstance(entry.get('receipt_paths'),list)
            or not entry['receipt_paths']):
        raise ValueError('v19_source_or_receipt_changed')
    value = projection(entry, current)
    source = session.scalar(select(Activity).where(Activity.workspace_id == workspace_id,
        Activity.lead_id == lead_id, Activity.id == event_id))
    if source is None:
        raise ValueError('v19_source_identity_changed')
    audits = list(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id,
        AuditEvent.entity_type == 'activity', AuditEvent.entity_id == event_id,
        AuditEvent.action.in_((ACTION,WITHDRAW))).order_by(AuditEvent.created_at,AuditEvent.id)))
    prior = current_audit(source, audits)
    before = str(prior.id) if prior else None
    receipt_key = f'v19:{event_id}:{expected_source_digest}:{entry["receipt_sha256"]}'
    receipt_id = uuid5(workspace_id, receipt_key)
    audit_id = uuid5(receipt_id,ACTION)
    existing = session.get(AuditEvent,audit_id)
    if existing is not None:
        if (existing.workspace_id != workspace_id or existing.entity_id != event_id
                or existing.details.get('classification_sha256') != digest(value)
                or existing.details.get('source_digest') != expected_source_digest):
            raise ValueError('v19_replay_collision')
        if before != str(audit_id):
            raise ValueError('v19_replay_superseded')
        return {'audit_id':str(audit_id),'receipt_id':str(receipt_id),'replayed':True}
    if before != expected_audit_id:
        raise ValueError('v19_audit_cas_conflict')
    if session.get(AgentWork,receipt_id) is not None:
        raise ValueError('v19_receipt_collision')
    receipt = {'version':VERSION,'status':'succeeded','simulated':False,
        'event_id':str(event_id),'source_digest':expected_source_digest,
        'source_sha256':entry['source_sha256'],'receipts_sha256':verified_receipts_sha256,
        'context_sha256':expected_context_sha256,
        'semantic_context_version':SEMANTIC_CONTEXT_VERSION,
        'semantic_context_sha256':semantic_context_hash(row),
        'model':entry['model'],'decisions':value,'before_audit_id':before}
    session.add(AgentWork(id=receipt_id,workspace_id=workspace_id,lead_id=lead_id,
        source_key=receipt_key,kind=KIND,status='completed',attempts=0,
        payload={'event_id':str(event_id),'source_digest':expected_source_digest},
        result=receipt,result_hash=digest(receipt)))
    session.add(AuditEvent(id=audit_id,workspace_id=workspace_id,command_id=audit_id,
        actor_id=principal.actor_id,entity_type='activity',entity_id=event_id,action=ACTION,
        details={'receipt_id':str(receipt_id),'source_digest':expected_source_digest,
            'policy_version':VERSION,'classification_sha256':digest(value),
            'before_audit_id':before,'original_activity_unchanged':True,
            'source_work_unchanged':True}))
    session.flush()
    return {'audit_id':str(audit_id),'receipt_id':str(receipt_id),'replayed':False,
            'before_audit_id':before}


def withdraw(session, principal, *, event_id, lead_id, target_audit_id, expected_source_digest):
    """Rollback only the exact current v19 audit; restore older audit by tombstone."""
    workspace_id=principal.workspace_id
    row=source_row(session,workspace_id,UUID(str(lead_id)),lock=True)
    source=session.scalar(select(Activity).where(Activity.workspace_id==workspace_id,
        Activity.lead_id==UUID(str(lead_id)),Activity.id==UUID(str(event_id))))
    if (source is None or row['lead']['id']!=str(lead_id)
            or source_digest(source)!=expected_source_digest):
        raise ValueError('v19_rollback_source_changed')
    records=list(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id==workspace_id,
        AuditEvent.entity_type=='activity',AuditEvent.entity_id==source.id,
        AuditEvent.action.in_((ACTION,WITHDRAW))).order_by(AuditEvent.created_at,AuditEvent.id)))
    target=next((r for r in records if str(r.id)==str(target_audit_id)),None)
    if (target is None or target.action!=ACTION or target.details.get('policy_version')!=VERSION
            or target.details.get('source_digest')!=expected_source_digest):
        raise ValueError('v19_rollback_target_changed')
    tombstone_id=uuid5(target.id,WITHDRAW)
    previous=session.get(AuditEvent,tombstone_id)
    if previous is not None:
        if (previous.workspace_id!=workspace_id or previous.details.get('target_audit_id')!=str(target.id)):
            raise ValueError('v19_rollback_collision')
        return {'audit_id':str(tombstone_id),'replayed':True}
    current=current_audit(source,records)
    if current is None or current.id!=target.id:
        raise ValueError('v19_rollback_cas_conflict')
    session.add(AuditEvent(id=tombstone_id,workspace_id=workspace_id,command_id=tombstone_id,
        actor_id=principal.actor_id,entity_type='activity',entity_id=source.id,
        action=WITHDRAW,details={'target_audit_id':str(target.id),
            'source_digest':expected_source_digest,'original_activity_unchanged':True,
            'source_work_unchanged':True}))
    session.flush()
    return {'audit_id':str(tombstone_id),'replayed':False}
