"""Read-only obligation status; queue completion is not email sending."""
from uuid import UUID
from sqlalchemy import select
from src.crm.persistence.models import Task, AgentWork


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
    return {**item,'processing':{'state':state,'obligations':obligations,
        'meaning':'internal_preparation_only; never proof of sending'}}
