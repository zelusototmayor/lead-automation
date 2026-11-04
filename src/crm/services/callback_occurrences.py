"""Occurrence ledger in canonical AgentWork payloads, committed with task intent.

No task-version/event identity coupling: annotation/version-only work reuses its
prior occurrence. A newly cited obligation gets a new immutable source identity.
The enqueue transaction persists the ID before any provider request is possible.
"""
from datetime import UTC
from uuid import uuid5
from sqlalchemy import select, Integer
from src.crm.persistence.models import AgentWork


def occurrence_for_enqueue(session, task, source_key, *, intent_key=None, content=None):
    due=task.due_at.astimezone(UTC).isoformat()
    prior=session.scalar(select(AgentWork).where(AgentWork.workspace_id==task.workspace_id,
        AgentWork.task_id==task.id,AgentWork.kind=='calendar_callback',AgentWork.source_key!=source_key)
        .order_by(AgentWork.payload['task_version'].astext.cast(Integer).desc(),
                  AgentWork.created_at.desc(),AgentWork.id.desc()).limit(1))
    previous=dict((prior.payload or {}).get('callback_occurrence') or {}) if prior else {}
    if prior and not previous:
        evidence=[e for e in (prior.result or {}).get('evidence',[]) if
                  e.get('provider')=='google_calendar' and e.get('verified') and e.get('event_id')]
        if prior.status=='completed' and len(evidence)==1 and evidence[0].get('due_at'):
            from datetime import datetime
            previous={'event_id':evidence[0]['event_id'],
                'due_at':datetime.fromisoformat(evidence[0]['due_at']).astimezone(UTC).isoformat(),
                'projected':True}
        elif intent_key is None:
            # No speculative migration of old unprojected/recovered intents.
            return {'migration_required':True}
    if previous and (task.status!='open' or
            (previous.get('due_at')==due and (intent_key is None or previous.get('intent_key')==intent_key))):
        return previous
    return {'event_id':'crm'+uuid5(task.id,'callback-occurrence:'+(intent_key or source_key)).hex,
            'due_at':due,'intent_key':intent_key or source_key,
            **({'content':dict(content)} if content is not None else {})}
