"""Read-only binding to immutable operational recovery receipts.

Does not grant note-plan rescheduling authority or mutate recovery/work history.
"""
from uuid import UUID
from sqlalchemy import select
from src.crm.persistence.models import AuditEvent,Activity
from src.crm.services.note_source_service import source_digest


def recovery_content(session,task):
    from src.crm.services.callback_execution import CalendarProjectionError
    recovery=session.scalar(select(AuditEvent).where(
        AuditEvent.workspace_id==task.workspace_id,AuditEvent.entity_type=='task',
        AuditEvent.entity_id==task.id,AuditEvent.action.in_(
            ['agent.callback_recovery_requested','agent.callback_recovered_from_source']))
        .order_by(AuditEvent.created_at.desc(),AuditEvent.id.desc()).limit(1))
    if recovery is None:return None
    # A subsequently committed canonical note plan supersedes this binding.
    newer=session.scalar(select(AuditEvent.id).where(
        AuditEvent.workspace_id==task.workspace_id,AuditEvent.action=='agent.note_interpreted',
        AuditEvent.details['task_ids'].contains([str(task.id)]),AuditEvent.created_at>recovery.created_at).limit(1))
    if newer:return None
    proof=recovery.details or {}
    try:
        activity=session.get(Activity,UUID(proof['source_activity_id']))
        valid=(recovery.action=='agent.callback_recovered_from_source' and proof.get('verified') is True
            and proof.get('provider')=='google_calendar' and 0<int(proof['task_version'])<=task.version
            and activity and activity.workspace_id==task.workspace_id and activity.lead_id==task.lead_id
            and source_digest(activity)==proof['source_digest'])
    except (KeyError,TypeError,ValueError):valid=False
    if not valid:raise CalendarProjectionError('Callback recovery source binding requires reconciliation')
    return activity.summary or ''
