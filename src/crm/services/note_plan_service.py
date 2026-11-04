"""Canonical cited-note writer. No caller-supplied provider receipts.

Validate the whole plan under work/lead/contact/task fences before provider I/O.
Gmail intent survives SQL rollback on the configured durable journal volume;
uncertain creation is reconciled, never blindly repeated. No send is reachable.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo
from sqlalchemy import select, or_, and_
from sqlalchemy.exc import OperationalError
from src.crm.persistence.models import Activity, AuditEvent, Lead, Task, Contact, Account
from src.crm.domain.call_contract import CallDetails
from src.crm.services.note_source_service import note_context
from src.crm.services.agent_work_service import (
    locked_work, validate_lease, finish_work, lead_is_suppressed, WorkConflict,
)

FACT_FIELDS = {'answer_kind','useful','decision_maker','interlocutor_role','repeat_reason'}


def _generated_lead_justification(item):
    # LeadOperationService.add_note(note_kind='lead_justification') emits this
    # exact typed Activity. It is bookkeeping, not a new customer instruction.
    # Missing origin, human notes, other system notes and facts stay blocking.
    return (item.get('activity_type') == 'note'
            and item.get('actor_type') == 'system'
            and item.get('source_system') == 'agent'
            and item.get('title') == 'Lead justification'
            and isinstance(item.get('summary'), str)
            and item['summary'].startswith('Justificação de lead: ')
            and not item.get('outcome_code') and not item.get('call_details')
            and not item.get('supersedes_activity_id'))


def _substantive_newer_context(source):
    """Return newer context rows that carry human/business evidence.

    Empty placeholder notes can be recorded around a call before the cited note is
    analyzed. They do not supersede a quoted callback request. Calls, emails,
    meetings, and notes with any evidence still force explicit reconciliation.
    """
    context = source.get('newer_context') or {}
    if context.get('has_more'):
        return [dict(reason='newer_context_truncated')]
    items = context.get('items') or []
    return [item for item in items if not _generated_lead_justification(item)
            and (item.get('activity_type') != 'note'
                 or bool(item.get('summary') or item.get('outcome_code') or item.get('call_details')))]


def _task_completion_is_consumed_source_history(session, task, source_at):
    """Completed stale call task recorded while entering this note.

    José's CRM UI can emit a task-history activity (for example ``Voltar a
    ligar``) when an old/manual callback task is consumed during the same human
    note entry. That history says the old CRM task was closed; it is not
    evidence that the requested new callback happened, and it must not block the
    replacement callback grounded in the actual call note.
    """
    if not (task.task_type == 'call'
            and task.completed_at and task.completion_activity_id and task.due_at < source_at
            and timedelta(0) <= task.completed_at - source_at <= timedelta(minutes=15)
            and task.source_rule in {'release:legacy_callback','manual_next_action','human_call_callback'}):
        return False
    completion = session.scalar(select(Activity).where(
        Activity.workspace_id == task.workspace_id,
        Activity.id == task.completion_activity_id,
        Activity.lead_id == task.lead_id,
        Activity.activity_type == 'task',
    ))
    if completion is None:
        return False
    title = (task.title or '').strip()
    summary = (completion.summary or completion.title or '').strip()
    return bool(title and summary and title in summary)


def _task_blocks_new_action(session, task, action, source):
    if task.id == action.existing_task_id:
        return False
    if task.status != 'completed':
        return True
    source_activity_id = UUID(source['activity_id'])
    if task.completion_activity_id == source_activity_id:
        return False
    source_at = datetime.fromisoformat(source['occurred_at'])
    # Legacy callback tasks are auto-consumed while recording the very call that
    # produced the note-plan source. They are old obligations, not a newer human
    # instruction, so they must not block the replacement callback from the same
    # call. Keep this narrow: old legacy callback, already overdue at the source,
    # and completed immediately after that source was recorded.
    if _task_completion_is_consumed_source_history(session, task, source_at):
        return False
    return True


def _adopt_existing_callback_task(task, action, source):
    source_at = datetime.fromisoformat(source['occurred_at'])
    if not (action.task_type == 'call' and task.status == 'open'
            and task.source_rule in {'release:legacy_callback','manual_next_action','human_call_callback'}
            and task.due_at < source_at and action.due_at > datetime.now(UTC)):
        raise WorkConflict('Existing task date requires explicit rescheduling')
    task.title = action.title
    task.due_at = action.due_at
    task.source_rule = 'head_of_sales_note'


def _existing_email_draft(action, task=None):
    """Preparing a draft may satisfy an overdue manual task without redating it."""
    if not (action.task_type == 'email' and action.draft is not None
            and action.existing_task_id and action.expected_task_version is not None):
        return False
    if task is None:
        return True  # Candidate only; the locked task must pass the check below.
    return bool(task.id == action.existing_task_id and task.status == 'open'
        and task.version == action.expected_task_version and task.due_at == action.due_at
        and task.task_type in {'email','follow_up'}
        and task.source_rule in {'manual_next_action','head_of_sales_note'})


def _resolve_existing_action_task(task, action, source, lead_id):
    if (not task or task.lead_id != lead_id or task.status != 'open'
            or task.version != action.expected_task_version):
        raise WorkConflict('Task changed, closed or mismatched')
    if (action.task_type == 'call'
            and (task.due_at != action.due_at or task.title != action.title)):
        _adopt_existing_callback_task(task, action, source)
    return task


def _new_note_callback_task(session, principal, work_id, lead, action, source, tasks):
    since = datetime.fromisoformat(source['occurred_at'])
    source_activity_id = UUID(source['activity_id'])
    # A new note creates a new obligation. Old Calendar events are
    # historical records, never repurposed as the next callback.
    if action.existing_task_id:
        prior = next((t for t in tasks if t.id == action.existing_task_id), None)
        if (prior is None or prior.lead_id != lead.id or prior.status != 'open'
                or prior.version != action.expected_task_version):
            raise WorkConflict('Task changed, closed or mismatched')
    for prior in tasks:
        if prior.status != 'open':
            continue
        changed_after_note = prior.created_at > since or (
            prior.updated_at is not None and prior.updated_at > since
            and prior.id != action.existing_task_id)
        if prior.lead_id != lead.id or changed_after_note:
            raise WorkConflict('Newer or sibling callback requires reconciliation')
        prior.status = 'completed' if prior.due_at <= since else 'cancelled'
        prior.completed_at = datetime.now(UTC) if prior.status == 'completed' else None
        prior.completion_activity_id = source_activity_id if prior.status == 'completed' else None
        prior.version += 1
        prior.updated_at = datetime.now(UTC)
        # No Calendar write: keep the original event unchanged.
    task_id = uuid5(work_id, 'note-action:' + action.key)
    task = Task(id=task_id, workspace_id=principal.workspace_id,
        lead_id=lead.id, account_id=lead.account_id, task_type='call',
        title=action.title, due_at=action.due_at, owner_user_id=principal.actor_id,
        status='open', source_rule='head_of_sales_note', call_intent=None)
    session.add(task)
    return task


def _align_callback_due_at(due_at, *, timezone='Europe/Lisbon'):
    """Round CRM callback starts onto José's allowed quarter-hour grid."""
    zone = ZoneInfo(timezone)
    local = due_at.astimezone(zone).replace(microsecond=0)
    extra_minutes = (15 - local.minute % 15) % 15
    if extra_minutes or local.second:
        local = local.replace(second=0) + timedelta(minutes=extra_minutes)
        if not extra_minutes:
            local += timedelta(minutes=15)
    return local.astimezone(UTC)


def _overdue_exact_callback(action, source, activity, *, now=None):
    """Preserve a missed explicit same-day human callback, never invent a new date."""
    import re
    now = now or datetime.now(UTC)
    if (action.task_type != 'call' or action.existing_task_id
            or action.due_at > now or activity is None
            or activity.activity_type != 'call' or activity.actor_type != 'human'
            or activity.source_system != 'manual'
            or str(activity.id) != source.get('activity_id')):
        return False
    source_at = datetime.fromisoformat(source['occurred_at'])
    zone = ZoneInfo('Europe/Lisbon')
    due = action.due_at.astimezone(zone)
    if (action.due_at < source_at or due.date() != source_at.astimezone(zone).date()
            or due.second or due.microsecond):
        return False
    if re.search(r'amanh|pr[oó]xim|semana|segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado|domingo|\d{1,2}[/\-]\d{1,2}',
                 action.quote, re.IGNORECASE):
        return False  # A separate-day reference needs explicit reconciliation.
    # The current source/quote and newer-context fences run before this helper.
    # Only an exact clock written in that quote authorizes the past slot.
    times = re.findall(r'(?<![\d\w])(\d{1,2})(?:h([0-5]\d)?|:([0-5]\d))(?![\d\w])',
                       action.quote, re.IGNORECASE)
    return any(int(hour) == due.hour and int(hminute or cminute or '0') == due.minute
               for hour, hminute, cminute in times)


def _normalize_callback_action(action):
    if action.task_type != 'call':
        return
    action.due_at = _align_callback_due_at(action.due_at)


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
    except Exception as exc:
        from src.crm.connectors.gmail_cooldown import GmailRateLimited
        if isinstance(exc, GmailRateLimited): raise
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


def apply_note_plan(session, principal, work_id, body, *, projection_only=False, _recalibration=None):
    if _recalibration is not None:
        from src.crm.services.jev_recalibration import _RECALIBRATION_AUTHORITY, RecalibrationBody
        if (not projection_only or _recalibration is not _RECALIBRATION_AUTHORITY
                or not isinstance(body, RecalibrationBody)):
            raise WorkConflict('Explicit recalibration command required')
    row = locked_work(session, principal.workspace_id, work_id)
    semantic = body.model_dump(mode='json', exclude={'lease_token','expected_lead_version'})
    # Preserve historical replay hashes for callers without Jev provenance.
    if semantic.get('jev_receipt_id') is None:
        semantic.pop('jev_receipt_id', None)
    # New optional features must not alter historical replay fingerprints.
    if not semantic.get('contacts'):semantic.pop('contacts',None)
    for change in semantic.get('contacts',[]):
        if change.get('title') is None:change.pop('title',None)
    for action in semantic.get('actions',[]):
        draft=action.get('draft')
        if draft:
            for key in ('additional_to','cc','include_presentation','thread_id'):
                if not draft.get(key) and draft.get(key) is not False:draft.pop(key,None)
    plan_hash = hashlib.sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if not projection_only and row.status == 'completed' and row.last_lease_token == body.lease_token:
        if (row.result or {}).get('plan_hash') != plan_hash:
            raise WorkConflict('Plan changed')
        return finish_work(session,principal.workspace_id,work_id,body.lease_token,result=row.result)
    if not projection_only:
        validate_lease(row,body.lease_token)
        from src.crm.services.moveauto_operational_admission import validate_plan
        validate_plan(session, principal, row, body)
    if row.kind != 'call_followup':
        raise WorkConflict('Not a note work item')
    lead, company_lead_ids = _locked_company_lead(session, principal.workspace_id, row.lead_id)
    if lead.version != body.expected_lead_version:
        raise WorkConflict('Lead changed')
    contacts = _locked_rows(session, select(Contact).where(Contact.workspace_id==principal.workspace_id,
        Contact.id==lead.contact_id)) if lead.contact_id else []
    contact = contacts[0] if contacts else None
    source = note_context(session,principal.workspace_id,lead.id,row.payload)
    if source.get('status') != 'current' or source.get('source_digest') != body.source_digest:
        raise WorkConflict('Source changed')
    from src.crm.services.call_note_stage import project_note_stage, read_note_stage, retained_callback_intent
    origin = session.get(Activity, UUID(source['activity_id']))
    stage_projection = (project_note_stage(session, lead, origin, principal.actor_id)
                        if not projection_only else read_note_stage(session, lead, origin))
    planned_actions = getattr(body, 'actions', [])
    retained_callback = bool(not projection_only and retained_callback_intent(origin))
    cited_callbacks = bool(planned_actions and retained_callback and all(
        a.task_type == 'call' and a.quote.strip() and a.quote in (source.get('summary') or '')
        for a in planned_actions))
    if stage_projection and stage_projection['status'] == 'blocked':
        raise WorkConflict('Explicit stage instruction requires reconciliation: ' + stage_projection['reason'])
    if retained_callback and not cited_callbacks:
        raise WorkConflict('Explicit retained callback requires a cited call action')
    # A synchronous closure must still let this exact note finish its CRM work.
    # It never unsuppresses email sending or actions from another source.
    own_closure = bool(stage_projection and stage_projection.get('target_stage') == lead.stage
                       and stage_projection['status'] in ('projected', 'already_projected')
                       and (not planned_actions or cited_callbacks)
                       and (not projection_only or not getattr(body, 'contacts', [])))
    if lead_is_suppressed(session,lead) and not (own_closure or cited_callbacks):
        raise WorkConflict('Lead suppressed')
    from src.crm.services.moveauto_callback_reconciliation import validate_binding, consume_canonical
    reconciliation = validate_binding(session, principal, row, lead, body, source, _locked_rows)
    if reconciliation:
        if projection_only:
            raise WorkConflict('Callback reconciliation requires the leased note writer')
        consumed = consume_canonical(session, principal, row, body, source,
                                     reconciliation, plan_hash, _locked_rows)
        if consumed is not None:
            return consumed
    jev, interest = None, None
    if body.jev_receipt_id is not None:
        from src.crm.services.jev_note_writer_bridge import bind_receipt
        jev = bind_receipt(session, principal.workspace_id, row, lead, body.jev_receipt_id, factual=projection_only)
    if projection_only:
        from src.crm.services.jev_note_worker import _decision_row
        from src.crm.services.jev_call_facts import VERSION, V2_VERSION, V3_VERSION, cited_facts, cited_interest
        from dashboard.app.routers.agent_work import NotePlanBody
        receipt_row = _decision_row(session, principal.workspace_id, str(body.jev_receipt_id))
        decisions, policy_version = receipt_row.result['decisions'], receipt_row.result['version']
        audit_id = uuid5(body.jev_receipt_id, 'note-facts-audit')
        audit_command_id = uuid5(body.jev_receipt_id, 'note-facts')
        if _recalibration is not None:
            from src.crm.services.jev_recalibration import prepare, TARGET_POLICY
            decisions, review = prepare(session, receipt_row, body)
            policy_version = TARGET_POLICY
            jev = dict(jev, applied_policy_version=TARGET_POLICY, recalibration=review)
            audit_id = uuid5(body.jev_receipt_id, 'jev-recalibration:v3-to-v4')
            audit_command_id = body.command_id
            prior_command = session.scalar(select(AuditEvent.id).where(
                AuditEvent.workspace_id == principal.workspace_id, AuditEvent.command_id == audit_command_id))
            if prior_command is not None and prior_command != audit_id:
                raise WorkConflict('Recalibration command already used')
        facts = cited_facts(source, decisions)
        if policy_version in (V2_VERSION, V3_VERSION, VERSION):
            interest = cited_interest(source, decisions, policy_version=policy_version)
        # Server constructs the plan from the bound receipt; no caller supplied facts/actions.
        body = NotePlanBody(lease_token=UUID(int=0), jev_receipt_id=body.jev_receipt_id,
            expected_lead_version=body.expected_lead_version, source_digest=body.source_digest,
            facts=facts, actions=[], summary='Jev cited call interpretation; obligations unchanged.')
        previous = session.get(AuditEvent, audit_id)
        if previous is not None:
            if previous.workspace_id != principal.workspace_id or previous.details.get('plan_hash') != plan_hash:
                raise WorkConflict('Factual projection changed')
            return {'facts': previous.details['facts'], 'jev': jev, 'replayed': True,
                    'expressed_commercial_interest': previous.details.get('expressed_commercial_interest'),
                    'source_work_unchanged': True, 'audit_id': str(audit_id)}
    text = source.get('summary') or ''
    disposition = body.disposition
    stage_change = None
    if disposition:
        from src.crm.domain.stage_policy import is_terminal_stage, highest_stage_rank, requires_account
        if (not disposition.quote.strip() or disposition.quote not in text
                or _substantive_newer_context(source)):
            raise WorkConflict('Disposition conflicts with current human context')
        target = disposition.target_stage
        if stage_projection and stage_projection.get('target_stage'):
            if stage_projection['target_stage'] != target:
                raise WorkConflict('Disposition contradicts explicit human note')
        else:
            # Preserve the existing connected-call route. Free text authorizes
            # another stage only through the current human command grammar.
            if (source.get('activity_type') != 'call' or source.get('outcome_code') != 'connected'
                    or target != 'contacted' or lead.stage != 'new'
                    or (source.get('call_details') or {}).get('answer_kind') in ('no_answer','voicemail','automated_system')):
                raise WorkConflict('Explicit matching human stage instruction required')
            stage_change = {'from_stage': lead.stage, 'to_stage': target, 'quote': disposition.quote}
        if is_terminal_stage(target) and body.actions and not cited_callbacks:
            raise WorkConflict('Closed leads cannot create followups')
    newer = _substantive_newer_context(source)
    if body.actions and newer and reconciliation is None:
        # A later outgoing email does not revoke an explicit independent callback.
        # Replies, changed notes/calls and meetings still require reconciliation.
        if not (all(a.task_type=='call' for a in body.actions)
                and all(item.get('activity_type')=='email_sent' for item in newer)):
            raise WorkConflict('Newer contact context requires reconciliation; no action from old note')
    if body.contacts and newer:
        raise WorkConflict('Newer contact data requires reconciliation')
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
    overdue_callback_keys=set()
    keys=set()
    # Bound a transaction's provider lifetime below its 600s lease. The model
    # must combine one email obligation, not create a series of similar drafts.
    if sum(a.task_type=='call' for a in body.actions)>1:
        raise WorkConflict('One callback obligation per note plan')
    if sum(a.task_type=='email' for a in body.actions)>1:
        raise WorkConflict('One email obligation per note plan')
    for action in body.actions:
        _normalize_callback_action(action)
        if action.key in keys or action.quote not in text or not action.quote.strip():
            raise WorkConflict('Invalid action evidence')
        keys.add(action.key)
        overdue_callback = False
        if action.due_at <= datetime.now(UTC) and not _existing_email_draft(action):
            overdue_callback = _overdue_exact_callback(action, source,
                session.get(Activity, UUID(source['activity_id'])))
            if not overdue_callback:
                raise WorkConflict('Action date must be in the future')
            overdue_callback_keys.add(action.key)
        if (action.task_type=='email') != (action.draft is not None):
            raise WorkConflict('Email requires draft content; no other action accepts it')
        if action.draft:
            recipient = str(contact.primary_email) if contact and contact.primary_email else lead.contact_email
            from src.crm.connectors.note_drafts import valid_address
            known={str(c.primary_email) for c in session.scalars(select(Contact).where(
                Contact.workspace_id==principal.workspace_id,Contact.account_id==lead.account_id)) if c.primary_email}
            known.add(recipient)
            addresses=[action.draft.recipient,*action.draft.additional_to,*action.draft.cc]
            if len({a.casefold() for a in addresses})!=len(addresses):
                raise WorkConflict('Duplicate draft recipient')
            for address in addresses:
                valid_address(address)
                if address not in known and address not in text:
                    raise WorkConflict('Recipient not evidenced literally')
        # Reconcile company obligations, including older tasks finished/edited
        # after the source. Nullable denormalized account_id is not an escape.
        task_scope = Task.lead_id.in_(company_lead_ids)
        if lead.account_id:
            task_scope = or_(task_scope, Task.account_id == lead.account_id)
        since = datetime.fromisoformat(source['occurred_at'])
        matching_type = Task.task_type == action.task_type
        if _existing_email_draft(action):
            # Adopt only the exact cited manual follow_up task, never a sibling
            # or ordinary sequence task; retain its type, deadline and history.
            matching_type = or_(matching_type, and_(Task.id == action.existing_task_id,
                Task.task_type == 'follow_up', Task.source_rule == 'manual_next_action'))
        tasks = _locked_rows(session, select(Task).where(
            Task.workspace_id == principal.workspace_id, task_scope,
            matching_type,
            or_(Task.status == 'open', Task.created_at >= since,
                Task.updated_at >= since, Task.completed_at >= since),
        ).order_by(Task.id))
        source_activity_id = UUID(source['activity_id'])
        if action.task_type == 'call':
            task = _new_note_callback_task(session, principal, work_id, lead, action, source, tasks)
        else:
            if any(_task_blocks_new_action(session, t, action, source) for t in tasks):
                raise WorkConflict('Existing company task must be reconciled explicitly')
            if action.existing_task_id:
                existing_task = next((t for t in tasks if t.id == action.existing_task_id), None)
                task = _resolve_existing_action_task(existing_task, action, source, lead.id)
            else:
                task_id=uuid5(work_id,'note-action:'+action.key)
                task=Task(id=task_id,workspace_id=principal.workspace_id,lead_id=lead.id,account_id=lead.account_id,
                    task_type=action.task_type,title=action.title,due_at=action.due_at,
                    owner_user_id=principal.actor_id,status='open',source_rule='head_of_sales_note',
                    call_intent=None)
                session.add(task)
        if (task.due_at <= datetime.now(UTC) and not _existing_email_draft(action, task)
                and not (overdue_callback and task.due_at == action.due_at and task.status == 'open')):
            raise WorkConflict('Existing task date requires explicit rescheduling')
        if task.task_type != action.task_type and not _existing_email_draft(action, task):
            raise WorkConflict('Existing task type requires explicit reconciliation')
        prepared.append((action,task))
    from src.crm.services.note_contacts import apply_contacts
    contact_changes=apply_contacts(session,principal,lead,work_id,source,body.contacts)
    if retained_callback:
        # The quoted exception's task exists now. Its creation is newer than
        # the source, so closure cancels old obligations while preserving it.
        stage_projection = project_note_stage(session, lead, origin, principal.actor_id,
            allow_retained_callback=True,
            retained_callback_task_ids=tuple(task.id for action, task in prepared
                                             if action.task_type == 'call'))
        if stage_projection['status'] not in ('projected', 'already_projected'):
            raise WorkConflict('Retained callback stage alignment is unconfirmed')
    if stage_projection and stage_projection.get('target_stage'):
        if stage_projection['actual_stage'] != stage_projection['target_stage']:
            raise WorkConflict('Explicit stage instruction was not applied')
    # Flush all SQL validation before any external effect. Audit excludes draft
    # bodies and uses compact evidence to respect the canonical 4096-byte limit.
    audit_details={'work_id':str(work_id),'source_digest':body.source_digest,'plan_hash':plan_hash,
        'facts':updates,'citations':[x.model_dump(mode='json') for x in body.facts],
        'action_keys':[x.key for x in body.actions],
        'task_ids':[str(t.id) for _,t in prepared],
        'schedule_basis':'internal_preparation_not_customer_agreement'}
    if reconciliation:
        audit_details['callback_reconciliation'] = reconciliation
    if overdue_callback_keys:
        audit_details['overdue_callback_keys'] = sorted(overdue_callback_keys)
        audit_details['overdue_policy'] = 'preserve_exact_human_time_open_obligation'
    if contact_changes:
        # Keep each contact's complete prior values separately, within audit limits.
        for index,change in enumerate(contact_changes):
            session.add(AuditEvent(id=uuid5(work_id,'note-contact-audit:'+str(index)),
                workspace_id=principal.workspace_id,command_id=uuid5(work_id,'note-contact:'+str(index)),
                actor_id=principal.actor_id,action='agent.contact_from_note',entity_type='contact',
                entity_id=UUID(change['contact_id']),details=change))
        audit_details['contact_ids']=[c['contact_id'] for c in contact_changes]
    if jev is not None:
        audit_details['jev'] = jev
    if interest is not None:
        audit_details['expressed_commercial_interest'] = interest
    if stage_projection and stage_projection.get('audit_id'):
        audit_details['stage_projection_audit_id'] = stage_projection['audit_id']
    if stage_change:
        lead.stage = stage_change['to_stage']
        lead.highest_stage_rank = highest_stage_rank(lead.highest_stage_rank, lead.stage)
        lead.updated_at = datetime.now(UTC)
        audit_details['stage_change'] = stage_change
        session.add(Activity(id=uuid5(work_id, 'note-stage-change'),
            workspace_id=principal.workspace_id, account_id=lead.account_id, lead_id=lead.id,
            activity_type='stage_change', occurred_at=datetime.now(UTC), title='Stage from call note',
            from_stage=stage_change['from_stage'], to_stage=lead.stage,
            source_system='agent', actor_type='agent', actor_id=principal.actor_id,
            semantic_fingerprint=plan_hash))
    if len(json.dumps(audit_details,ensure_ascii=False).encode())>3800:
        raise WorkConflict('Citation evidence exceeds audit budget')
    session.flush()
    if projection_only:
        session.add(AuditEvent(id=audit_id, workspace_id=principal.workspace_id,
            command_id=audit_command_id, actor_id=principal.actor_id,
            action='agent.note_interpreted', entity_type='activity',
            entity_id=UUID(source['activity_id']), details=audit_details))
        session.flush()
        return {'facts': updates, 'jev': jev, 'replayed': False,
                'expressed_commercial_interest': interest,
                'source_work_unchanged': True, 'audit_id': str(audit_id)}
    task_refs=[]
    for action,task in prepared:
        validate_lease(row,body.lease_token)
        receipt=draft_for_action(workspace_id=principal.workspace_id,task=task,action=action,source=source) if action.draft else None
        from src.crm.services.agent_work_service import enqueue_callback
        from src.crm.services.callback_execution import callback_content
        callback_id=enqueue_callback(session,task,
            intent_key='note:'+source['activity_id']+':'+action.key,
            content={'company_name':callback_content(session,task)['company_name'],
                     'call_notes':source.get('summary') or ''}
        ) if action.task_type=='call' else None
        task_refs.append({'task_id':str(task.id),'task_type':task.task_type,'status':task.status,
            'calendar_work_id':str(callback_id) if callback_id else None,
            'calendar_status':'pending' if callback_id else 'not_required',
            'due_at':task.due_at.astimezone(UTC).isoformat() if task.due_at else None,
            'draft_status':'verified' if receipt else 'not_required','draft':receipt,
            'overdue': action.key in overdue_callback_keys})
    result={'summary':body.summary,'plan_hash':plan_hash,'facts':updates,'tasks':task_refs,
        'evidence':[{'activity_id':source['activity_id'],'source_digest':body.source_digest}],
        'next_action':None,'external_actions':'draft_only' if any(a.draft for a in body.actions) else 'none'}
    if stage_projection and stage_projection.get('audit_id'):
        result['stage_change'] = {k: stage_projection[k] for k in (
            'from_stage', 'to_stage', 'source_activity_id', 'basis', 'quote', 'audit_id')}
        result['stage_alignment'] = {
            'expected_stage': stage_projection['target_stage'], 'actual_stage': lead.stage,
            'source_activity_id': source['activity_id'], 'verified': lead.stage == stage_projection['target_stage'],
            **stage_projection.get('task_alignment', {})}
    elif stage_change:
        result['stage_change'] = {**stage_change, 'source_activity_id': source['activity_id']}
        result['stage_alignment'] = {'expected_stage':stage_change['to_stage'],
            'actual_stage':lead.stage, 'source_activity_id':source['activity_id'],
            'verified':lead.stage == stage_change['to_stage']}
    if reconciliation:
        result['evidence'] = reconciliation['evidence']
        result['callback_reconciliation'] = reconciliation
    session.add(AuditEvent(id=uuid5(work_id,'note-plan-audit'),workspace_id=principal.workspace_id,
        command_id=uuid5(work_id,'note-plan'),actor_id=principal.actor_id,action='agent.note_interpreted',
        entity_type='activity',entity_id=UUID(source['activity_id']),details=audit_details))
    if jev is not None:
        result['jev'] = jev
    if contact_changes:
        result['contacts']=contact_changes
    elif not projection_only:
        from src.crm.services.note_contacts import latest_contact_phase
        phase=latest_contact_phase(session,principal.workspace_id,work_id,source)
        if phase and phase.get('verified'):
            result['contact_phase']={k:phase[k] for k in ('command_id','work_id',
                'source_activity_id','source_digest','lead_version','contacts','verified')}
            result['contacts']=[{'contact_id':c['contact_id'],'version':c['version'],
                'source_activity_id':source['activity_id']} for c in phase['actual_changes']]
    if body.actions:
        lead.updated_at=datetime.now(UTC)
    session.flush()
    return finish_work(session,principal.workspace_id,work_id,body.lease_token,result=result)
