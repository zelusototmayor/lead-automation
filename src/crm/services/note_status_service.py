"""Read-only obligation status; queue completion is not email sending."""
from uuid import UUID
from sqlalchemy import select
from src.crm.persistence.models import Task, AgentWork, Lead, Activity


def current_stage_alignment(session, workspace_id, item):
    """Read actual effects of an operative human note, never mutate business data.

    A completed queue row is not proof that the requested tag or cancellation
    happened. Later genuine human context retires this historical requirement.
    """
    from src.crm.services.call_note_stage import stage_intent, newer_human_context, read_note_stage, explicit_stage_intent
    recorded = (item.get('result') or {}).get('stage_alignment') or {}
    payload = item.get('payload') or {}
    if (not recorded and payload.get('outcome_code')!='not_interested'
            and not explicit_stage_intent(payload.get('summary'))):
        return None
    try:
        activity_id = UUID((item.get('payload') or {})['activity_id'])
        lead_id = UUID(item['lead_id'])
    except (KeyError, TypeError, ValueError):
        return ({'operative':True, 'verified_current':False, 'unfulfilled':['source_unavailable']}
                if recorded else None)
    source = session.scalar(select(Activity).where(Activity.workspace_id==workspace_id,
        Activity.id==activity_id, Activity.lead_id==lead_id))
    if source is None:
        return ({'operative':True, 'verified_current':False, 'unfulfilled':['source_unavailable']}
                if recorded else None)
    intent = stage_intent(source)
    if not intent:
        return None
    expected = intent['target_stage']
    lead = session.scalar(select(Lead).where(Lead.workspace_id==workspace_id, Lead.id==lead_id))
    if lead is None:
        return {'operative':True, 'expected_stage':expected, 'actual_stage':None,
                'verified_current':False, 'unfulfilled':['lead_unavailable']}
    if newer_human_context(session, lead, source):
        return {'operative':False, 'status':'superseded_by_later_human_context',
                'expected_stage':expected, 'actual_stage':lead.stage,
                'verified_current':None, 'unfulfilled':[]}
    unfulfilled = []
    if lead.stage != expected:
        unfulfilled.append('stage')
    # Recovery can repair a previously completed source without rewriting its
    # original receipt. The canonical per-source audit owns retained callbacks
    # and pending Calendar-cancellation refs, tied to the exact source digest.
    try:
        proof = read_note_stage(session, lead, source)
    except ValueError:
        return {'operative':True, 'expected_stage':expected, 'actual_stage':lead.stage,
                'verified_current':False, 'unfulfilled':['source_proof_conflict']}
    canonical = (proof or {}).get('task_alignment') or {}
    retained = set(canonical.get('preserved_current_callback_task_ids', []))
    incompatible = []
    if expected in ('lost', 'not_a_fit', 'won', 'meeting_booked'):
        from src.crm.services.call_note_alignment import superseded_action_filters
        incompatible = [value for value in session.scalars(select(Task.id).where(
            *superseded_action_filters(workspace_id, lead_id, source, expected)))
            if str(value) not in retained]
        if incompatible:
            unfulfilled.append('next_actions')
    saved = recorded.get('task_alignment', recorded)
    refs = set(canonical.get('calendar_cancellation_work_ids', [])) | set(
        saved.get('calendar_cancellation_work_ids', []))
    calendars, pending, blocked = [], False, False
    for value in sorted(refs):
        try:
            work = session.get(AgentWork, UUID(value))
        except (ValueError, TypeError):
            work = None
        valid = bool(work and work.workspace_id==workspace_id and work.lead_id==lead_id
                     and work.kind=='calendar_callback')
        verified = bool(valid and work.status=='completed' and any(
            e.get('provider')=='google_calendar' and e.get('verified') is True
            and e.get('event_id') and e.get('status') in ('cancelled','history_preserved')
            for e in (work.result or {}).get('evidence', [])))
        status = 'verified' if verified else (work.status if valid else 'missing')
        calendars.append({'work_id':value, 'status':status, 'verified_current':verified})
        if not verified:
            pending |= valid and work.status in ('queued','running')
            blocked |= not valid or work.status not in ('queued','running')
    if pending or blocked:
        unfulfilled.append('calendar_cancellations')
    return {'operative':True, 'expected_stage':expected, 'actual_stage':lead.stage,
            'verified_current':not unfulfilled, 'unfulfilled':unfulfilled,
            'incompatible_open_task_ids':[str(value) for value in incompatible],
            'calendar_cancellations':calendars,
            'pending_only':unfulfilled==['calendar_cancellations'] and pending and not blocked}


def with_note_processing(session, workspace_id, item, *, verify_drafts=False):
    if item['kind']!='call_followup':
        return item
    state={'queued':'pending','running':'processing','waiting':'blocked',
           'failed':'failed','completed':'processed'}.get(item['status'],'blocked')
    obligations=[]
    for ref in (item.get('result') or {}).get('tasks',[]):
        task=session.scalar(select(Task).where(Task.workspace_id==workspace_id,
            Task.id==UUID(ref['task_id']),Task.lead_id==UUID(item['lead_id'])))
        entry={**ref,'task_status':task.status if task else 'missing'}
        if not task:
            state='blocked'
        elif task.status!='open':
            entry['disposition']='closed_by_current_crm_state'
            # A closed task is not proof of an email send. Do not reopen it.
        elif ref.get('calendar_work_id'):
            current=session.scalar(select(AgentWork).where(AgentWork.workspace_id==workspace_id,
                AgentWork.task_id==task.id,AgentWork.kind=='calendar_callback',
                AgentWork.payload['task_version'].as_integer()==task.version)
                .order_by(AgentWork.created_at.desc()).limit(1))
            evidence=(current.result or {}).get('evidence',[]) if current else []
            verified=current and current.status=='completed' and any(
                e.get('provider')=='google_calendar' and e.get('verified') is True and e.get('event_id') for e in evidence)
            entry['calendar_status']='verified' if verified else 'pending'
            if not verified and state=='processed':state='partial'
            if current and current.status in ('failed','waiting'):state='blocked'
        if entry.get('draft'):
            entry['draft_status']='verified_at_apply_not_sent'
            if verify_drafts and task and task.status=='open':
                try:
                    import json,os
                    from pathlib import Path
                    from src.crm.connectors.note_drafts import GmailDraftProvider,create_verified_draft
                    root=Path(os.environ.get('CRM_NOTE_DRAFT_JOURNAL_DIR',''))
                    if not root.is_absolute():raise ValueError('Durable journal unavailable')
                    journal=root/str(workspace_id)/(str(task.id)+'.json')
                    intent=json.loads(journal.read_text())['intent']
                    mailbox=os.environ.get('CRM_GOOGLE_MAILBOX','')
                    if intent['mailbox']!=mailbox or mailbox!='zelu@zelusottomayor.com':raise ValueError('Mailbox mismatch')
                    checked=create_verified_draft(provider=GmailDraftProvider(os.environ['GOOGLE_GMAIL_CREDENTIALS_FILE'],mailbox),
                        journal=journal,key=str(workspace_id)+':'+str(task.id),reconcile_only=True,**intent)
                    if checked!=entry['draft']:raise ValueError('Receipt changed')
                    entry['draft_status']='verified_current_not_sent'
                except Exception:
                    entry['draft_status']='unconfirmed';state='blocked'
        obligations.append(entry)
    alignment = current_stage_alignment(session, workspace_id, item)
    if alignment and alignment.get('operative') and not alignment.get('verified_current'):
        state = 'partial' if alignment.get('pending_only') and state == 'processed' else 'blocked'
    processing = {'state':state, 'obligations':obligations,
        'meaning':'internal_preparation_only; never proof of sending'}
    if alignment is not None:
        processing['stage_alignment'] = alignment
    return {**item, 'processing':processing}
