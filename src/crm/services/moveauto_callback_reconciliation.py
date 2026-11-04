"""One finite, reviewed reconciliation; not an inference or scheduling API.

The two immutable MoveAuto sources express the same return-date obligation.
Only the original note work may create it. The call work can consume the exact
committed canonical receipt, never create another task or provider operation.
"""
from datetime import UTC, datetime
from uuid import UUID, uuid5
from sqlalchemy import select, or_
from src.crm.persistence.models import Activity, AgentWork, AuditEvent, Lead, Task
from src.crm.services.agent_work_service import WorkConflict, finish_work
from src.crm.services.note_source_service import source_digest

BINDING = 'moveauto24-return-20261104-v1'
WORKSPACE = UUID('339f8411-5023-4b6c-ae0e-202609050001')
LEAD = UUID('4f578a60-69dd-58cc-8ec9-8abc9a01ebb7')
ACCOUNT = UUID('d2e5897e-aa92-573a-8999-c0d5a529a231')
PRIMARY = UUID('0e29f0c4-17ed-5920-8fa2-65963310d4e9')
SECONDARY = UUID('37a0b88b-7ce2-5671-9bd7-2b38ff1deb32')
NOTE = UUID('d643e8d3-a8f9-52ac-a1b7-e8ee259be796')
CALL = UUID('102f4fb3-ff90-5d02-a39b-10b3f89d3c1d')
SOURCES = {
    NOTE: ('note', '0f265f24d1ce55eb39a41fabc48f2a10fd3983c63798b9905cff1e340d8180ce',
           '2026-10-20T15:00:36.540251+00:00'),
    CALL: ('call', '3d4e2fe295c364e8a6e6a234d3698eacde45284317d914df3961d7d201740abd',
           '2026-10-20T15:00:52.838969+00:00'),
}
DUE = datetime(2026, 11, 4, 10, tzinfo=UTC)
KEY = 'reconcile_moveauto24_return_20261104_v1'
TITLE = 'Ligar a Fernando após o regresso'


def validate_binding(session, principal, row, lead, body, source, locked_rows):
    # The existing action-key contract is the explicit opt-in. No new worker,
    # helper capability, alternative writer or schema change is required.
    if not any(a.key == KEY for a in getattr(body, 'actions', [])):
        return None
    if (principal.workspace_id != WORKSPACE
            or lead.id != LEAD or lead.account_id != ACCOUNT
            or row.id not in (PRIMARY, SECONDARY) or row.lead_id != LEAD
            or UUID(source['activity_id']) != (NOTE if row.id == PRIMARY else CALL)
            or body.facts or body.contacts or body.disposition or body.jev_receipt_id
            or len(body.actions) != 1):
        raise WorkConflict('Callback reconciliation binding mismatch')
    action = body.actions[0]
    if (action.key != KEY or action.task_type != 'call' or action.due_at != DUE
            or action.title != TITLE or action.quote != source.get('summary')
            or action.existing_task_id or action.expected_task_version is not None
            or action.draft is not None):
        raise WorkConflict('Callback reconciliation action changed')
    # Parent/company locks fence inserts. Lock the original activities as well,
    # so an in-place source edit cannot race digest validation and commit.
    rows = locked_rows(session, select(Activity).where(
        Activity.workspace_id == WORKSPACE,
        or_(Activity.lead_id.in_(select(Lead.id).where(Lead.workspace_id == WORKSPACE,
            Lead.account_id == ACCOUNT)), Activity.account_id == ACCOUNT),
        # Human/manual context is not limited to communication activity types.
        # Keep the existing broad communication fence as well; either origin
        # marker must block an unbound human instruction (including on siblings).
        or_(Activity.activity_type.in_(('note', 'call', 'email_sent', 'email_received', 'meeting')),
            Activity.actor_type == 'human', Activity.source_system == 'manual'),
    ).order_by(Activity.id))
    if {a.id for a in rows} != set(SOURCES):
        raise WorkConflict('Callback reconciliation context changed')
    for activity in rows:
        kind, digest, recorded = SOURCES[activity.id]
        if (activity.lead_id != LEAD or activity.account_id != ACCOUNT
                or activity.activity_type != kind or activity.actor_type != 'human'
                or activity.source_system != 'manual' or source_digest(activity) != digest
                or activity.created_at != datetime.fromisoformat(recorded)):
            raise WorkConflict('Callback reconciliation source changed')
    # Supersession of either source is blocking even when the new row has a
    # different activity type or company association.
    if session.scalar(select(Activity.id).where(Activity.workspace_id == WORKSPACE,
            Activity.supersedes_activity_id.in_(SOURCES)).limit(1)):
        raise WorkConflict('Callback reconciliation source superseded')
    if row.id == PRIMARY and locked_rows(session, select(Task).where(
            Task.workspace_id == WORKSPACE,
            or_(Task.lead_id.in_(select(Lead.id).where(Lead.workspace_id == WORKSPACE,
                Lead.account_id == ACCOUNT)), Task.account_id == ACCOUNT))):
        raise WorkConflict('Callback reconciliation requires the verified empty task context')
    return {'binding': BINDING, 'canonical_work_id': str(PRIMARY),
            'evidence': [{'activity_id': str(k), 'source_digest': v[1]}
                         for k, v in SOURCES.items()]}


def consume_canonical(session, principal, row, body, source, binding, plan_hash, locked_rows):
    """Secondary only: finish with an actual task receipt, not an empty plan."""
    if row.id == PRIMARY:
        return None
    primary = session.get(AgentWork, PRIMARY, populate_existing=True)
    result = primary.result if primary is not None else None
    if (primary is None or primary.workspace_id != WORKSPACE or primary.lead_id != LEAD
            or primary.kind != 'call_followup' or primary.status != 'completed'
            or not result or result.get('callback_reconciliation') != binding
            or result.get('evidence') != binding['evidence']):
        raise WorkConflict('Canonical note work must commit the bound callback first')
    tasks = locked_rows(session, select(Task).where(Task.workspace_id == WORKSPACE,
        or_(Task.lead_id.in_(select(Lead.id).where(Lead.workspace_id == WORKSPACE,
            Lead.account_id == ACCOUNT)), Task.account_id == ACCOUNT)).order_by(Task.id))
    refs = result.get('tasks') or []
    expected_id = uuid5(PRIMARY, 'note-action:' + KEY)
    if len(tasks) != 1 or len(refs) != 1:
        raise WorkConflict('Canonical callback task context changed')
    task = tasks[0]
    if (task.id != expected_id or task.lead_id != LEAD or task.account_id != ACCOUNT
            or task.status != 'open' or task.task_type != 'call' or task.due_at != DUE
            or task.title != TITLE or task.source_rule != 'head_of_sales_note'
            or refs[0].get('task_id') != str(expected_id)
            or not refs[0].get('calendar_work_id')):
        raise WorkConflict('Canonical callback task changed')
    callback = session.get(AgentWork, UUID(refs[0]['calendar_work_id']), populate_existing=True)
    if (callback is None or callback.workspace_id != WORKSPACE
            or callback.task_id != expected_id or callback.kind != 'calendar_callback'):
        raise WorkConflict('Canonical Calendar obligation missing')
    reused = dict(result, plan_hash=plan_hash, summary=body.summary,
                  canonical_receipt_reused=True)
    session.add(AuditEvent(id=uuid5(row.id, 'note-plan-audit'), workspace_id=WORKSPACE,
        command_id=uuid5(row.id, 'note-plan'), actor_id=principal.actor_id,
        action='agent.note_interpreted', entity_type='activity', entity_id=CALL,
        details={'work_id': str(row.id), 'source_digest': body.source_digest,
                 'plan_hash': plan_hash, 'task_ids': [str(expected_id)],
                 'callback_reconciliation': binding, 'canonical_receipt_reused': True}))
    session.flush()
    return finish_work(session, WORKSPACE, row.id, body.lease_token, result=reused)
