"""Canonical cited-note writer. No caller-supplied provider receipts.

Validate the whole plan under work/lead/contact/task fences before provider I/O.
Gmail intent survives SQL rollback on the configured durable journal volume;
uncertain creation is reconciled, never blindly repeated. No send is reachable.
"""
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid5
from sqlalchemy import select, or_
from sqlalchemy.exc import OperationalError
from src.crm.persistence.models import Activity, AuditEvent, Lead, Task, Contact, Account
from src.crm.domain.call_contract import CallDetails
from src.crm.services.note_source_service import note_context
from src.crm.services.agent_work_service import (
    locked_work, validate_lease, finish_work, lead_is_suppressed, WorkConflict,
)

FACT_FIELDS = {'answer_kind','useful','decision_maker','interlocutor_role','repeat_reason'}


def draft_for_action(*, workspace_id, task, action, source):
    from src.crm.connectors.note_drafts import GmailDraftProvider, create_verified_draft
    mailbox = os.environ.get('CRM_GOOGLE_MAILBOX', '')
    credentials = os.environ.get('GOOGLE_GMAIL_CREDENTIALS_FILE', '')
    journal_root = os.environ.get('CRM_NOTE_DRAFT_JOURNAL_DIR', '')
    # No fallback to ephemeral /tmp or a worker credential transport.
    if mailbox != 'zelu@zelusottomayor.com' or not credentials or not Path(journal_root).is_absolute():
        raise WorkConflict('Server draft capability unavailable; preserve obligation')
    try:
        return create_verified_draft(
            provider=GmailDraftProvider(credentials, mailbox),
            journal=Path(journal_root)/str(workspace_id)/(str(task.id)+'.json'),
            key=str(workspace_id)+':'+str(task.id), mailbox=mailbox,
            since=source['occurred_at'], **action.draft.model_dump())
    except Exception:
        raise WorkConflict('Draft unconfirmed; journal retained; no repeat create') from None


def _locked_rows(session, query):
    """Fail closed on a competing writer; never wait in reverse lock order."""
    try:
        return list(session.scalars(query.with_for_update(nowait=True)
                                   .execution_options(populate_existing=True)))
    except OperationalError as exc:
        if getattr(exc.orig, 'sqlstate', None) == '55P03':
            raise WorkConflict('Company context is being edited; retry reconciliation') from None
        raise


def _locked_company_lead(session, workspace_id, lead_id):
    # Account first serializes sibling note plans, even if there are no tasks.
    # Full row locks also fence FK-backed activity/task inserts. NOWAIT avoids
    # deadlocking a manual writer that acquired a lead/task before its account.
    identity = session.execute(select(Lead.account_id).where(
        Lead.workspace_id == workspace_id, Lead.id == lead_id)).first()
    if identity is None:
        raise WorkConflict('Lead changed')
    account_id = identity.account_id
    if account_id:
        if not _locked_rows(session, select(Account).where(
                Account.workspace_id == workspace_id, Account.id == account_id)):
            raise WorkConflict('Company changed')
    rows = _locked_rows(session, select(Lead).where(
        Lead.workspace_id == workspace_id,
        or_(Lead.id == lead_id, Lead.account_id == account_id) if account_id else Lead.id == lead_id,
    ).order_by(Lead.id))
    lead = next((item for item in rows if item.id == lead_id), None)
    if not lead or lead.account_id != account_id:
        raise WorkConflict('Company identity changed')
    return lead, [item.id for item in rows]


def apply_note_plan(session, principal, work_id, body):
    row = locked_work(session, principal.workspace_id, work_id)
    semantic = body.model_dump(mode='json', exclude={'lease_token','expected_lead_version'})
    plan_hash = hashlib.sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if row.status == 'completed' and row.last_lease_token == body.lease_token:
        if (row.result or {}).get('plan_hash') != plan_hash:
            raise WorkConflict('Plan changed')
        return finish_work(session,principal.workspace_id,work_id,body.lease_token,result=row.result)
    validate_lease(row,body.lease_token)
    if row.kind != 'call_followup':
        raise WorkConflict('Not a note work item')
    lead, company_lead_ids = _locked_company_lead(session, principal.workspace_id, row.lead_id)
    if lead.version != body.expected_lead_version:
        raise WorkConflict('Lead changed')
    contacts = _locked_rows(session, select(Contact).where(Contact.workspace_id==principal.workspace_id,
        Contact.id==lead.contact_id)) if lead.contact_id else []
    contact = contacts[0] if contacts else None
    if lead_is_suppressed(session,lead):
        raise WorkConflict('Lead suppressed')
    source = note_context(session,principal.workspace_id,lead.id,row.payload)
    if source.get('status') != 'current' or source.get('source_digest') != body.source_digest:
        raise WorkConflict('Source changed')
    text = source.get('summary') or ''
    if body.actions and source.get('newer_context',{}).get('items'):
        raise WorkConflict('Newer contact context requires reconciliation; no action from old note')
    existing = source.get('call_details') or {}
    updates = {}
    for fact in body.facts:
        if fact.field not in FACT_FIELDS or fact.field in updates or not fact.quote.strip() or fact.quote not in text:
            raise WorkConflict('Invalid fact evidence')
        if source['activity_type'] != 'call':
            raise WorkConflict('Facts require call source')
        prior = existing.get(fact.field)
        if prior not in (None,'unknown') and prior != fact.value:
            raise WorkConflict('Human facts win')
        updates[fact.field] = fact.value
    if updates:
        merged = dict(schema_version=1,attempted=True,answer_kind='unknown',useful=None,
            decision_maker=None,interlocutor_role='unknown',repeat_reason=None)
        merged.update(existing);merged.update(updates)
        details = CallDetails.model_validate(merged)
        details.validate_outcome(source.get('outcome_code'))
        if source.get('outcome_code') == 'connected' and details.answer_kind not in {'unknown','human_counterparty'}:
            raise WorkConflict('Call outcome conflict')
        if details.interlocutor_role == 'reception' and details.decision_maker is True:
            raise WorkConflict('Reception is not decision maker evidence')
    prepared=[]
    keys=set()
    # Bound a transaction's provider lifetime below its 600s lease. The model
    # must combine one email obligation, not create a series of similar drafts.
    if sum(a.task_type=='email' for a in body.actions)>1:
        raise WorkConflict('One email obligation per note plan')
    for action in body.actions:
        if action.key in keys or action.quote not in text or not action.quote.strip():
            raise WorkConflict('Invalid action evidence')
        keys.add(action.key)
        if action.due_at <= datetime.now(UTC):
            raise WorkConflict('Action date must be in the future')
        if (action.task_type=='email') != (action.draft is not None):
            raise WorkConflict('Email requires draft content; no other action accepts it')
        if action.draft:
            recipient = str(contact.primary_email) if contact and contact.primary_email else lead.contact_email
            if action.draft.recipient != recipient and action.draft.recipient not in text:
                raise WorkConflict('Recipient not evidenced literally')
            from src.crm.connectors.note_drafts import valid_address
            valid_address(action.draft.recipient)
        # Reconcile company obligations, including older tasks finished/edited
        # after the source. Nullable denormalized account_id is not an escape.
        task_scope = Task.lead_id.in_(company_lead_ids)
        if lead.account_id:
            task_scope = or_(task_scope, Task.account_id == lead.account_id)
        since = datetime.fromisoformat(source['occurred_at'])
        tasks = _locked_rows(session, select(Task).where(
            Task.workspace_id == principal.workspace_id, task_scope,
            Task.task_type == action.task_type,
            or_(Task.status == 'open', Task.created_at >= since,
                Task.updated_at >= since, Task.completed_at >= since),
        ).order_by(Task.id))
        if any(t.id != action.existing_task_id for t in tasks):
            raise WorkConflict('Existing company task must be reconciled explicitly')
        if action.existing_task_id:
            task = next((t for t in tasks if t.id == action.existing_task_id), None)
            if (not task or task.lead_id != lead.id or task.status!='open'
                    or task.version!=action.expected_task_version):
                raise WorkConflict('Task changed, closed or mismatched')
        else:
            task_id=uuid5(work_id,'note-action:'+action.key)
            task=Task(id=task_id,workspace_id=principal.workspace_id,lead_id=lead.id,account_id=lead.account_id,
                task_type=action.task_type,title=action.title,due_at=action.due_at,
                owner_user_id=principal.actor_id,status='open',source_rule='head_of_sales_note',
                call_intent=None)
            session.add(task)
        if task.due_at <= datetime.now(UTC):
            raise WorkConflict('Existing task date requires explicit rescheduling')
        prepared.append((action,task))
    # Flush all SQL validation before any external effect. Audit excludes draft
    # bodies and uses compact evidence to respect the canonical 4096-byte limit.
    audit_details={'work_id':str(work_id),'source_digest':body.source_digest,'plan_hash':plan_hash,
        'facts':updates,'citations':[x.model_dump(mode='json') for x in body.facts],
        'action_keys':[x.key for x in body.actions],
        'task_ids':[str(t.id) for _,t in prepared],
        'schedule_basis':'internal_preparation_not_customer_agreement'}
    if len(json.dumps(audit_details,ensure_ascii=False).encode())>3800:
        raise WorkConflict('Citation evidence exceeds audit budget')
    session.flush()
    task_refs=[]
    for action,task in prepared:
        validate_lease(row,body.lease_token)
        receipt=draft_for_action(workspace_id=principal.workspace_id,task=task,action=action,source=source) if action.draft else None
        from src.crm.services.agent_work_service import enqueue_callback
        callback_id=enqueue_callback(session,task) if action.task_type=='call' else None
        task_refs.append({'task_id':str(task.id),'task_type':task.task_type,'status':task.status,
            'calendar_work_id':str(callback_id) if callback_id else None,
            'calendar_status':'pending' if callback_id else 'not_required',
            'due_at':task.due_at.astimezone(UTC).isoformat() if task.due_at else None,
            'draft_status':'verified' if receipt else 'not_required','draft':receipt})
    result={'summary':body.summary,'plan_hash':plan_hash,'facts':updates,'tasks':task_refs,
        'evidence':[{'activity_id':source['activity_id'],'source_digest':body.source_digest}],
        'next_action':None,'external_actions':'draft_only' if any(a.draft for a in body.actions) else 'none'}
    session.add(AuditEvent(id=uuid5(work_id,'note-plan-audit'),workspace_id=principal.workspace_id,
        command_id=uuid5(work_id,'note-plan'),actor_id=principal.actor_id,action='agent.note_interpreted',
        entity_type='activity',entity_id=UUID(source['activity_id']),details=audit_details))
    if body.actions:
        lead.updated_at=datetime.now(UTC)
    session.flush()
    return finish_work(session,principal.workspace_id,work_id,body.lease_token,result=result)
