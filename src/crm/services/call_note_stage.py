"""Apply explicit current human stage instructions, retaining the source note.

This is a small command grammar, not a qualification classifier. Descriptions,
negations and a future/conditional closure do not become immediate commands.
The caller owns the lead lock, transaction and later provider projection.
"""
from __future__ import annotations

import re
from uuid import uuid5

POLICY = 'human-call-stage/2026-10-30-v1'
_ALIASES = {
    'new': r'new|nov[ao]',
    'contacted': r'contacted|contactad[ao]|contatad[ao]',
    'qualified': r'qualified|qualificad[ao]',
    'meeting_booked': r'meeting[_ ]booked|reuni[aã]o marcada|reuni[aã]o agendada',
    'meeting_held': r'meeting[_ ]held|reuni[aã]o realizada',
    'proposal_requested': r'proposal[_ ]requested|proposta pedida|proposta solicitada',
    'proposal_sent': r'proposal[_ ]sent|proposta enviada',
    'negotiation': r'negotiation|negocia[cç][aã]o',
    'won': r'won|ganh[ao]',
    'lost': r'lost|perdid[ao]',
    'not_a_fit': r'not[_ ]a[_ ]fit|sem perfil|fora do perfil|n[aã]o [ée] fit',
}
_TARGET = '|'.join(f'(?P<{name}>{aliases})' for name, aliases in _ALIASES.items())
_LEAD = r'(?:a |esta |essa |a esta |a essa )?lead'
_PREFIX = (
    rf'(?:{_LEAD}\s+(?:(?:j[aá]\s+)?(?:est[aá]|[ée]|t[aá]|fica)\s+(?:(?:em|como)\s+)?|[ée]\s+dar\s+como\s+)'
    rf'|{_LEAD}\s*[:=]\s*'
    rf'|(?:dar|marcar|classificar|considerar|colocar|meter|passar)\s+(?:{_LEAD}\s+)?(?:(?:como|em|para)\s+)?'
    rf'|(?:consider|mark)\s+(?:this(?: lead)?|the lead)\s+(?:as\s+)?'
    rf'|(?:reabrir|reativar)\s+(?:{_LEAD}\s+)?(?:como|em|para)\s+)'
)
_COMMAND = re.compile(rf'\b(?:{_PREFIX}(?P<stage>{_TARGET})|(?P<meeting_fact>reuni[aã]o (?:marcada|agendada|realizada)))\b', re.IGNORECASE)
_ANNOTATION = re.compile(
    rf'^\s*(?:(?:lead|estado|status|tag|label)(?:\s*[:=]\s*|\s+))?'
    rf'(?P<stage>{_TARGET})\b(?=\s*(?:$|[-—–,;:!.\n]))', re.I)
_NEGATED = re.compile(r'\b(?:n[aã]o|nunca|sem)\s+(?:(?:h[aá]|tem(?:os)?|houve|existe|uma|[ée]|para|vamos|quero|vou|deves?|devemos)\s+){0,4}$', re.I)
_DEFERRED = re.compile(r'\b(?:depois|ap[oó]s|mais tarde|amanh[aã]|futuramente|eventualmente|quando|caso|se)\b', re.I)
_REOPEN = re.compile(r'\b(?:reabrir|reativar)\b', re.I)
_CALLBACK_TAIL = re.compile(r'\b(?:mas|apesar|mesmo assim|ainda)\b.*\b(?:ligar|chamada|telefonar)\b', re.I | re.S)


def explicit_stage_intent(text):
    """Return an exact cited current-state instruction, or no automatic change."""
    if not isinstance(text, str):
        return None
    found = []
    annotation = _ANNOTATION.search(text)
    matches = ([annotation] if annotation else []) + list(_COMMAND.finditer(text))
    for match in matches:
        before, after = text[:match.start()], text[match.end():]
        # Defer the whole command if its timing depends on another action.
        # Do not mistake 'não atende, dar a lead como lost' for a negation.
        clause_before = re.split(r'[.!;\n]', before)[-1]
        clause_after = re.split(r'[.!;\n]', after)[0]
        deferred_after = _DEFERRED.search(clause_after)
        callback_tail = _CALLBACK_TAIL.search(clause_after)
        if (_NEGATED.search(clause_before)
                or _DEFERRED.search(clause_before)
                or (deferred_after and (not callback_tail or deferred_after.start() < callback_tail.start()))
                or '?' in clause_after):
            continue
        if match.groupdict().get('meeting_fact'):
            # A confirmed meeting is an explicit commercial state. This does
            # not manufacture its missing clock time or a calendar event.
            email_prefix = re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', before.strip())
            if (clause_before.strip() and not email_prefix and not re.search(
                    r'(?:[,：:\-]\s*|\b(?:ficou|temos|confirmou|confirmada|j[aá])\s+)$', clause_before, re.I)):
                continue
            target = 'meeting_held' if re.search('realizada', match.group(0), re.I) else 'meeting_booked'
        else:
            target = next(name for name in _ALIASES if match.group(name) is not None)
        found.append({'target_stage': target, 'quote': match.group(0),
                      'basis': 'explicit_human_note',
                      'reviewed_correction': bool(_REOPEN.search(match.group(0)))})
    if not found or len({item['target_stage'] for item in found}) != 1:
        return None
    return found[-1]


def stage_intent(source):
    """Trust origin columns; a quotation by a model or provider is never authority."""
    if (source.actor_type != 'human' or source.source_system != 'manual'
            or source.activity_type not in ('call', 'note')
            or (source.activity_type == 'note' and source.title != 'Note added')):
        return None
    intent = explicit_stage_intent(source.summary)
    if intent:
        return intent
    if source.activity_type == 'call' and source.outcome_code == 'not_interested':
        return {'target_stage': 'lost', 'quote': None, 'basis': 'human_call_outcome',
                'reviewed_correction': False}
    return None


def retained_callback_intent(source):
    """A terminal instruction can explicitly retain a human callback, never mail."""
    intent = stage_intent(source)
    if not intent or intent['target_stage'] not in ('lost', 'not_a_fit') or not intent['quote']:
        return False
    tail = (source.summary or '').split(intent['quote'], 1)[-1]
    return bool(_CALLBACK_TAIL.search(tail))


def newer_human_context(session, lead, source):
    """Later genuine intent, excluding only this source's verified own effect."""
    from sqlalchemy import and_, or_, select
    from src.crm.persistence.models import Activity, AuditEvent
    from src.crm.services.note_source_service import source_digest

    own = session.get(AuditEvent, uuid5(source.id, POLICY))
    own_effect = None
    if (own and own.workspace_id == lead.workspace_id and own.entity_id == lead.id
            and own.details.get('source_digest') == source_digest(source)):
        command = uuid5(source.id, POLICY + ':transition')
        own_effect = uuid5(source.workspace_id, f'{command}:activity:lead.stage-transitioned')
    query = select(Activity.id).where(
        Activity.workspace_id == lead.workspace_id,
        or_(Activity.lead_id == lead.id,
            Activity.account_id == lead.account_id) if lead.account_id else Activity.lead_id == lead.id,
        Activity.id != source.id,
        or_(Activity.supersedes_activity_id == source.id,
            and_(Activity.actor_type == 'human', Activity.source_system == 'manual',
                 Activity.activity_type.in_(('call', 'note', 'stage_change')),
                 or_(Activity.occurred_at > source.occurred_at,
                     Activity.created_at > source.created_at)),
            and_(Activity.activity_type.in_(('email_received', 'meeting')),
                 Activity.occurred_at > source.occurred_at)),
    )
    if own_effect is not None:
        query = query.where(Activity.id != own_effect)
    return session.scalar(query.limit(1)) is not None


def read_note_stage(session, lead, source):
    """Read a same-source stage proof without mutation, including factual-only use."""
    from src.crm.persistence.models import AuditEvent
    from src.crm.services.note_source_service import source_digest

    audit_id = uuid5(source.id, POLICY)
    previous = session.get(AuditEvent, audit_id)
    if previous is None:
        return None
    if (previous.workspace_id != lead.workspace_id or previous.entity_id != lead.id
            or previous.details.get('source_digest') != source_digest(source)):
        raise ValueError('note_stage_projection_identity_changed')
    return {**previous.details, 'status': 'already_projected',
            'actual_stage': lead.stage, 'audit_id': str(audit_id)}


def project_note_stage(session, lead, source, actor_id, *, allow_retained_callback=False,
                       retained_callback_task_ids=()):
    """Apply once with source/current-state fences and native command services."""
    from src.crm.persistence.models import Activity, AuditEvent
    from src.crm.services.note_source_service import source_digest
    from src.crm.domain.stage_policy import is_terminal_stage

    intent = stage_intent(source)
    if not intent or source.workspace_id != lead.workspace_id or source.lead_id != lead.id:
        return {'status': 'not_requested', 'actual_stage': lead.stage}
    if retained_callback_intent(source) and not allow_retained_callback:
        return {'status': 'deferred_callback_plan', 'target_stage': intent['target_stage'],
                'actual_stage': lead.stage, 'source_activity_id': str(source.id)}
    if retained_callback_task_ids and not (allow_retained_callback and retained_callback_intent(source)):
        raise ValueError('Explicit retained callback instruction required')
    session.flush()
    digest, audit_id = source_digest(source), uuid5(source.id, POLICY)
    previous = read_note_stage(session, lead, source)
    if previous is not None:
        return previous
    # Later genuine human edits, calls or business replies take precedence.
    # Generated bookkeeping and this policy's own effect are not new intent.
    newer = newer_human_context(session, lead, source)
    target = intent['target_stage']
    if newer:
        return {'status': 'blocked', 'reason': 'newer_human_context',
                'target_stage': target, 'actual_stage': lead.stage}
    if is_terminal_stage(lead.stage) and lead.stage != target and not intent['reviewed_correction']:
        return {'status': 'blocked', 'reason': 'explicit_reopening_required',
                'target_stage': target, 'actual_stage': lead.stage}
    from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
    from src.crm.services.command_service import HumanCommandService, HumanCommandPrincipal, TransitionLeadCommand, CommandConflictError
    from src.crm.services.call_note_alignment import cancel_open_commercial_tasks
    # Borrow only: the outer caller remains the sole transaction owner.
    uow = SqlAlchemyUnitOfWork(lambda: session).__enter__()
    principal = HumanCommandPrincipal(source.actor_id or actor_id, lead.workspace_id,
                                     frozenset({'crm:lead-stage:write', 'crm:task:write'}))
    before = lead.stage
    try:
        with session.begin_nested():
            if before != target:
                HumanCommandService(uow).transition_lead(principal, TransitionLeadCommand(
                    command_id=uuid5(source.id, POLICY + ':transition'),
                    workspace_id=lead.workspace_id, lead_id=lead.id,
                    target_stage=target, expected_version=lead.version,
                    reviewed_correction=intent['reviewed_correction']))
            alignment = (cancel_open_commercial_tasks(session, lead, source, principal.actor_id,
                         retained_callback_task_ids=retained_callback_task_ids)
                         if is_terminal_stage(target) or target == 'meeting_booked' else {})
            details = {'policy': POLICY, 'source_activity_id': str(source.id),
                       'source_digest': digest, 'from_stage': before, 'to_stage': target,
                       'target_stage': target, 'basis': intent['basis'], 'quote': intent['quote'],
                       'task_alignment': alignment, 'send_authority': False}
            session.add(AuditEvent(id=audit_id, workspace_id=lead.workspace_id,
                command_id=audit_id, actor_id=principal.actor_id,
                action='lead.note_stage_projected', entity_type='lead', entity_id=lead.id,
                details=details))
            session.flush()
    except CommandConflictError:
        return {'status': 'blocked', 'reason': 'canonical_stage_or_task_conflict',
                'target_stage': target, 'actual_stage': lead.stage}
    return {**details, 'status': 'projected', 'actual_stage': lead.stage, 'audit_id': str(audit_id)}
