"""One durable preparation per workspace/day; never creates calls or tasks."""
import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, case
from src.crm.persistence.models import CallDayPlan, Workspace, Task, Lead, Activity


def build_call_day(session, workspace_id, work_date, capacity_minutes):
    # Reuse canonical suppression/contact predicates instead of a parallel CRM.
    from dashboard.app.routers.pipeline import _pipeline_statement, _to_item
    zone_name = session.scalar(select(Workspace.timezone).where(Workspace.id == workspace_id))
    zone = ZoneInfo(zone_name)
    start = datetime.combine(work_date, time.min, zone).astimezone(UTC)
    end = datetime.combine(work_date, time.max, zone).astimezone(UTC)
    future_ids = set(session.scalars(select(Task.lead_id).where(
        Task.workspace_id == workspace_id, Task.task_type == 'call',
        Task.status == 'open', Task.due_at > end)))
    attempted_ids = set(session.scalars(select(Activity.lead_id).where(
        Activity.workspace_id == workspace_id,
        Activity.activity_type == 'call',
        Activity.occurred_at >= start,
        Activity.occurred_at <= end)))
    entries, seen, seen_phones = [], set(), set()
    # All due obligations remain discoverable in canonical task queues. The plan
    # groups one counterparty into one work item without deleting any obligation.
    candidates = []
    for cohort in ('calls_actionable', 'phone_new', 'phone_unknown'):
        query = _pipeline_statement(workspace_id, cohort, start, end)
        if cohort == 'calls_actionable':
            query = query.order_by(Task.due_at, Task.id)
        else:
            query = query.order_by(case((Lead.priority == 'high', 0), (Lead.priority == 'medium',1), else_=2), Lead.id)
        for row in session.execute(query).all():
            candidates.append((cohort,row))
    budget = min(100, capacity_minutes // 5)
    deduplicated = 0
    for cohort,row in candidates:
        identity = str(row.account_id or row.lead_id)
        phone = re.sub(r'[^0-9]', '', row.phone or '')
        if cohort != 'calls_actionable' and (row.lead_id in future_ids or row.lead_id in attempted_ids):
            continue
        if identity in seen or (phone and phone in seen_phones):
            deduplicated += 1
            continue
        seen.add(identity)
        if phone:
            seen_phones.add(phone)
        if len(entries) >= budget:
            continue
        item = _to_item(row).model_dump(mode='json')
        entries.append(dict(item, cohort=cohort, reason=(
            'Obrigação existente: confirmar contexto e executar' if cohort == 'calls_actionable'
            else 'Prioridade CRM; confirmar se é primeira conversa ao registar'),
            history_status='unknown' if cohort == 'phone_unknown' else 'recorded',
            agreed_with_client=False, estimated_minutes=5))
    return dict(schema_version=1, work_date=work_date.isoformat(), date=work_date.isoformat(),
        timezone=zone_name, capacity_minutes=capacity_minutes, target_first_answered=10,
        items=entries, total=len(entries), available_counterparties=len(seen),
        deduplicated_candidates=deduplicated, source_status='partial',
        blockers=['Preparação interna baseada no CRM. Validar últimas mensagens antes de ligar; histórico desconhecido não é primeira conversa confirmada.'],
        new_tasks_created=0, calendar_events_created=0,
        generated_at=datetime.now(UTC).isoformat())


def prepare_call_day(session, principal, body):
    from fastapi import HTTPException
    # Serializes same-workspace preparation and protects exactly-once creation.
    session.scalar(select(Workspace).where(Workspace.id == principal.workspace_id).with_for_update())
    material = body.model_dump(mode='json')
    digest = hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    existing = session.get(CallDayPlan, (principal.workspace_id, body.work_date))
    if existing:
        if existing.command_id != body.command_id or existing.request_hash != digest or existing.actor_id != principal.actor_id:
            raise HTTPException(409, 'Command conflict')
        return dict(existing.payload, replayed=True)
    if body.expected_version != 0:
        raise HTTPException(409, 'Command conflict')
    payload = build_call_day(session, principal.workspace_id, body.work_date, body.capacity_minutes)
    payload.update(command_id=str(body.command_id), version=1)
    session.add(CallDayPlan(workspace_id=principal.workspace_id,work_date=body.work_date,
        command_id=body.command_id,actor_id=principal.actor_id,request_hash=digest,version=1,payload=payload))
    session.flush()
    return dict(payload,replayed=False)
