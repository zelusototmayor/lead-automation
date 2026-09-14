"""Immutable note sources for the existing Sales work queue.

No business inference happens here. The exact source snapshot is hashed without
Unicode normalization; interpretation lives separately from immutable Activity.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC
from uuid import UUID
from sqlalchemy import select, or_
from src.crm.persistence.models import Activity

from src.crm.services.agent_work_service import enqueue_work


def source_snapshot(activity):
    return {
        "activity_id": str(activity.id),
        "activity_type": activity.activity_type,
        "occurred_at": activity.occurred_at.astimezone(UTC).isoformat(),
        "summary": activity.summary,
        "outcome_code": activity.outcome_code,
        "call_details": activity.call_details,
        "supersedes_activity_id": str(activity.supersedes_activity_id) if activity.supersedes_activity_id else None,
    }


def source_digest(activity):
    encoded = json.dumps(source_snapshot(activity), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def enqueue_note_source(session, activity, *, source_key=None, callback_task_id=None):
    digest = source_digest(activity)
    return enqueue_work(
        session, workspace_id=activity.workspace_id, lead_id=activity.lead_id,
        source_key=source_key or f"note:{activity.id}:{digest}", kind="call_followup",
        payload={"activity_id": str(activity.id), "source_digest": digest,
                 "summary": activity.summary, "outcome_code": activity.outcome_code,
                 "callback_task_id": str(callback_task_id) if callback_task_id else None},
    )


def reconcile_note_sources(session, workspace_id, limit=50):
    """Durable per-source checkpoint, not a lossy timestamp window.

    A source is covered only once its queue disposition is committed. Failed or
    waiting work remains visible; reconciliation never reopens it blindly.
    """
    from sqlalchemy import cast, String, exists
    from sqlalchemy.orm import aliased
    from src.crm.persistence.models import AgentWork
    from src.crm.activity_provenance import operational_activity_filter
    newer=aliased(Activity)
    covered=exists(select(AgentWork.id).where(
        AgentWork.workspace_id==workspace_id,AgentWork.kind=='call_followup',
        AgentWork.payload['activity_id'].astext==cast(Activity.id,String)))
    superseded=exists(select(newer.id).where(newer.workspace_id==workspace_id,
        newer.supersedes_activity_id==Activity.id))
    query=select(Activity).where(Activity.workspace_id==workspace_id,
        Activity.lead_id.is_not(None),Activity.activity_type.in_(('call','note')),
        Activity.summary.is_not(None),Activity.summary!='',operational_activity_filter(),
        ~covered,~superseded).order_by(Activity.created_at,Activity.id)
    sources=list(session.scalars(query.limit(limit).with_for_update(skip_locked=True)))
    ids=[str(enqueue_note_source(session,source)) for source in sources]
    session.flush()
    more=session.scalar(query.limit(1)) is not None
    return {'enqueued':len(ids),'work_ids':ids,'has_more':more,
        'checkpoint':'committed_source_dispositions','coverage':'all_operational_notes'}


def note_context(session, workspace_id, lead_id, payload):
    try:
        activity_id = UUID(payload["activity_id"])
    except (KeyError, ValueError, TypeError):
        return {"status": "source_unavailable"}
    source = session.scalar(select(Activity).where(
        Activity.workspace_id == workspace_id, Activity.lead_id == lead_id,
        Activity.id == activity_id))
    if source is None:
        return {"status": "source_unavailable"}
    history = list(session.scalars(select(Activity).where(
        Activity.workspace_id == workspace_id,
        or_(Activity.lead_id == lead_id, Activity.account_id == source.account_id) if source.account_id else Activity.lead_id == lead_id,
        Activity.occurred_at < source.occurred_at,
        Activity.activity_type.in_(("call", "email_sent", "email_received", "meeting")),
    ).order_by(Activity.occurred_at.desc(), Activity.id).limit(101)))
    superseded = session.scalar(select(Activity.id).where(
        Activity.workspace_id == workspace_id, Activity.supersedes_activity_id == source.id).limit(1))
    digest = source_digest(source)
    newer_context=list(session.scalars(select(Activity).where(
        Activity.workspace_id==workspace_id,
        or_(Activity.lead_id == lead_id, Activity.account_id == source.account_id) if source.account_id else Activity.lead_id == lead_id,
        Activity.id!=source.id,
        # Event order and recording order can differ after delayed ingestion.
        # Equal timestamps are ambiguous, not proof that this context is older.
        or_(Activity.created_at >= source.created_at, Activity.occurred_at >= source.occurred_at),
        Activity.activity_type.in_(('note','call','email_sent','email_received','meeting'))
    ).order_by(Activity.created_at.desc(),Activity.id).limit(31)))
    return {**source_snapshot(source), "source_digest": digest,
        "newer_context":{"items":[source_snapshot(row) for row in newer_context[:30]],
            "has_more":len(newer_context)>30},
        "status": "superseded" if superseded else "current" if digest == payload.get("source_digest") else "source_changed",
        "history": {"items": [{**source_snapshot(row), "title": row.title} for row in history[:100]],
            "has_more": len(history) > 100, "prior_contact_known": bool(history),
            "all_channel_history_complete": False}}
