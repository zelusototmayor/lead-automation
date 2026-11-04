"""Source-backed daily preparation; no calls, tasks, stages or notes are written."""
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from uuid import UUID
from sqlalchemy import select, or_
from src.crm.persistence.models import CallDayPlan, Workspace, Task, Lead, Activity


def _plain(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text or '').casefold()
                   if not unicodedata.combining(c))


def _phone(text):
    digits = re.sub(r'[^0-9]', '', text or '')
    if digits.startswith('00'):
        digits = digits[2:]
    if len(digits) == 9:
        digits = '351' + digits
    return digits if 8 <= len(digits) <= 15 else None


# Narrow literal assertions, NOT a replacement contextual classifier. A number,
# answered call, DM role, callback, priority or stage never implies interest.
_INTEREST = re.compile(r'\b(?:mostrou interesse|demonstrou interesse|esta interessado|esta interessada|tem interesse|ficou interessado|ficou interessada)\b')
_REFUSAL = re.compile(r'\b(?:nao tem interesse|nao esta interessad[oa]|sem interesse|nao contactar|nao insistir|fora da ronda)\b')
_DEFER = re.compile(r'\b(?:aguardar (?:a )?(?:chamada deles|retorno deles|iniciativa|feedback)|aguardar\s*,\s*responsavel|nao repetir (?:a )?chamada|nao reabrir|nao contactar (?:agora|por enquanto))\b')
_STRATEGY_HOLDS = {'cargaquatro': 'Aguardar iniciativa/disponibilidade do cliente',
                   'transaire': 'Aguardar feedback após inventário'}

# Explicit channel predicates, not an email-word blacklist or a replacement
# contextual classifier. "email só com informação" describes the payload,
# whereas "tem que ser sempre por email" restricts the contact channel.
_CALL_CHANNEL_RESTRICTION = re.compile(
    r'\b(?:(?:so|apenas|exclusivamente) (?:pode|podem) (?:ser|contactar) por e-?mail'
    r'|(?:tem|t[eê]m) (?:que|de) ser (?:sempre |so |apenas |exclusivamente )?por e-?mail'
    r'|(?:contacto|contato|contactar|contactos|comunicacao) (?:so|apenas|exclusivamente) por e-?mail'
    r'|(?:so|apenas|exclusivamente) por e-?mail'
    r'|nao (?:aceita|aceitam|quer|querem|permite|permitem) (?:receber )?(?:chamadas|contacto telefonico)'
    r'|nao (?:voltar a )?ligar)\b')


def _call_channel_restricted(text):
    """Recognize only literal assertions; ambiguous context stays for review."""
    normalized = _plain(text)
    for match in _CALL_CHANNEL_RESTRICTION.finditer(normalized):
        prefix = normalized[max(0, match.start() - 100):match.start()]
        if not (re.search(r'\bnao\s*$', prefix) or re.search(r'\b(?:se|caso|perguntar|confirmar|sera que|nao e|nao tem (?:que|de) ser)\b[^.;!?\n]*$', prefix)):
            return True
    return False


def _context(sources, classifications=None):
    """Retain original source references and independent Jev dimensions."""
    result = {'interest': [], 'refusals': [], 'holds': [], 'call_restrictions': [], 'sources': []}
    for source in sources:
        text = source.summary or ''
        evidence = {'activity_id': str(source.id), 'source_identity_id': str(source.source_identity_id) if source.source_identity_id else None,
                    'occurred_at': source.occurred_at.isoformat(), 'actor_type': source.actor_type,
                    'recording_date_only': source.actor_type in ('migration', 'import', 'system'),
                    'original_excerpt': text[:600]}
        result['sources'].append(evidence)
        normalized = _plain(text)
        # Outbound templates and agent summaries are not client assertions.
        assertion_source = source.activity_type in ('note', 'call', 'email_received') and source.actor_type not in ('agent', 'automation', 'system')
        if assertion_source and _call_channel_restricted(text):
            result['call_restrictions'].append(evidence)
        negative_interest = re.search(r'\bnao (?:mostrou|demonstrou|tem|ficou) interesse|\bnao (?:ficou|esta) interessad[oa]', normalized)
        if assertion_source and _DEFER.search(normalized):
            result['holds'].append(evidence)
        if assertion_source and (_REFUSAL.search(normalized) or negative_interest or source.outcome_code == 'not_interested'):
            result['refusals'].append(evidence)
        elif assertion_source:
            match = _INTEREST.search(normalized)
            prefix = normalized[max(0, match.start()-40):match.start()] if match else ''
            if match and not re.search(r'\b(?:se|caso|nao|perguntar|confirmar|sera que)\b[^.;!?]*$', prefix):
                evidence['literal_signal'] = match.group(0)
                evidence['source_sha256'] = hashlib.sha256(text.encode()).hexdigest()
                result['interest'].append(evidence)
    result['jev'] = classifications or []
    return result


def _next_call(profile, context, history, end):
    """Consume categorical, source-bound canonical review, not keyword scores.

    An absent optional review preserves legacy selection; a stale review does
    not silently erase a known restriction. Fresh prospecting needs no prior interest.
    This is CALL-only; no email permission or send gate is changed.
    """
    field = profile.get('fields', {}).get('next_contact', {})
    route = field.get('value') if field.get('current_entity') else None
    # Append-only canonical revert-to-null restores original optional absence.
    # Keep stale non-null restrictions fail-closed; never erase their history.
    if field.get('current_entity') and field.get('value') is None:
        return None, None
    personal = [a for a in history if a.summary and (a.activity_type == 'email_received' or
        a.activity_type in ('note', 'call', 'meeting', 'proposal') and
        a.actor_type not in ('agent', 'automation', 'system', 'service'))]
    if not route:
        return None, 'next_contact_review_required' if field else None
    if route['channel'] == 'defer':
        return route, 'future_next_step'  # expiry is not fresh phone consent
    if route['channel'] != 'call':
        return route, 'next_channel_' + route['channel']
    if route.get('not_before') and datetime.fromisoformat(route['not_before'].replace('Z', '+00:00')) > end:
        return route, 'future_next_step'
    # A reviewed newer phone instruction may supersede an older literal ban.
    bound = {str(a.id): a for a in personal}
    latest = max((bound[r['activity_id']].occurred_at for r in route['source_refs']
                  if r['activity_id'] in bound), default=None)
    restrictions = context['call_restrictions'] + context['refusals'] + context['holds']
    if restrictions and (latest is None or any(datetime.fromisoformat(r['occurred_at']) >= latest for r in restrictions)):
        return route, 'original_call_channel_restriction'
    return route, None


def _financial(profile):
    field = profile.get('fields', {}).get('ebitda', {})
    value = field.get('value') or {}
    if not field.get('current_entity') or value.get('currency') != 'EUR':
        return None
    amount = Decimal(value['amount']) * {'units': 1, 'thousands': 1000, 'millions': 1000000}[value['unit']]
    return {'amount_eur': str(amount), 'year': value['year'], 'command_id': field['command_id'],
            'source_url': value['source_url'], 'source_sha256': value['source_sha256']}


def _contact_activity(source, classify=None):
    """Actual calls/emails, and owner's call notes; never preparation notes."""
    if source.actor_type in ('migration', 'import', 'system'):
        return False
    if source.activity_type in ('email_sent', 'email_received', 'meeting', 'proposal'):
        return True
    if source.activity_type == 'call':
        return True
    if source.activity_type != 'note':
        return False
    from src.crm.services.personal_call_rules import eligible, interaction
    if not eligible(source):
        return False
    # Read the canonical projection, including source-bound not-a-call evidence.
    if classify:
        projection = classify(source).get('classification') or {}
        units = projection.get('units') or []
        if units:
            return any(u['classification'].get('call', {}).get('value') is True for u in units)
    return interaction(source)['call']


def _uncertain_first_touch(source):
    """Unknown personal history is not first-touch proof.

    lead.details_updated emits an empty manual note via LeadOperationService;
    it edits identity fields, never records a contact. Match the full known
    administrative shape, not a word in arbitrary human/imported text.
    """
    if source.activity_type != 'note':
        return True  # includes migrated calls and imported emails
    if (getattr(source, 'source_system', None) == 'manual'
            and getattr(source, 'title', None) == 'Lead details updated'
            and source.actor_type == 'human' and not source.summary
            and not source.outcome_code and not getattr(source, 'call_details', None)
            and not getattr(source, 'direction', None)):
        return False
    return source.actor_type not in ('system', 'agent', 'automation', 'service', 'migration', 'import')


def _company(text):
    return re.sub(r'[^a-z0-9]', '', _plain(text))


def select_contacts(candidates, target_contacts):
    """Due obligations, evidenced interest, follow-up, then reviewed fit.

    No quotas or synthetic scores. Among fresh prospects, less represented
    sectors first; within that sector reviewed EBITDA precedes unknown values.
    """
    ordered = sorted((c for c in candidates if c['cohort'] != 'fresh_fit'),
                     key=lambda c: (c['_rank'], c['_order'], c['lead_id']))
    fresh = [c for c in candidates if c['cohort'] == 'fresh_fit']
    sectors = Counter()
    while fresh:
        fresh.sort(key=lambda c: (sectors[c.get('company_type')],
            c['financial_evidence'] is None,
            -Decimal(c['financial_evidence']['amount_eur']) if c['financial_evidence'] else Decimal(0), c['lead_id']))
        item = fresh.pop(0)
        sectors[item.get('company_type')] += 1
        ordered.append(item)
    entries, identities, phones, companies = [], set(), set(), set()
    duplicates = 0
    for item in ordered:
        identity, phone = item['_identity'], item['_phone']
        company = _company(item.get('company'))
        if identity in identities or phone in phones or (company and company in companies):
            duplicates += 1
            continue
        identities.add(identity); phones.add(phone)
        if company: companies.add(company)
        if len(entries) < target_contacts:
            entries.append({k: v for k, v in item.items() if not k.startswith('_')})
    return entries, len(identities), duplicates


def build_call_day(session, workspace_id, work_date, capacity_minutes, target_contacts=40, *, enrich=True):
    from dashboard.app.routers.pipeline import _pipeline_statement, _to_item, _lead_suppressed
    from src.crm.activity_provenance import operational_activity_filter
    from src.crm.services.commercial_profile import read_profile
    from src.crm.services.jose_note_service import read_classification
    zone_name = session.scalar(select(Workspace.timezone).where(Workspace.id == workspace_id))
    zone = ZoneInfo(zone_name)
    start = datetime.combine(work_date, time.min, zone).astimezone(UTC)
    end = datetime.combine(work_date, time.max, zone).astimezone(UTC)
    prepared_at = datetime.now(UTC)
    previous = session.scalar(select(CallDayPlan).where(CallDayPlan.workspace_id == workspace_id,
        CallDayPlan.work_date < work_date).order_by(CallDayPlan.work_date.desc()).limit(1))
    previous_payload = previous.payload if previous else {}
    generated = previous_payload.get('generated_at')
    since = datetime.fromisoformat(generated.replace('Z', '+00:00')) if generated else prepared_at
    if since.tzinfo is None:
        raise ValueError('Previous plan timestamp requires timezone')
    previous_items = {i['lead_id']: i for i in previous_payload.get('items', [])}
    from dashboard.app.routers.pipeline import _legacy_contact_exists, _external_contact_exists
    known_contact_ids = set(session.scalars(select(Lead.id).where(
        Lead.workspace_id == workspace_id, or_(_legacy_contact_exists(workspace_id),
        _external_contact_exists(workspace_id)))))
    # Equivalence uses current lead/account, normalized company and phone.
    # Union whole components so alias chains cannot evade protections.
    leads = list(session.scalars(select(Lead).where(Lead.workspace_id == workspace_id)))
    parent = {lead.id: lead.id for lead in leads}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]; key = parent[key]
        return key
    aliases = {}
    for lead in leads:
        for key in (('account', lead.account_id), ('company', _company(lead.company_name)),
                    ('phone', _phone(lead.contact_phone))):
            if not key[1]: continue
            if key in aliases: parent[root(lead.id)] = root(aliases[key])
            else: aliases[key] = lead.id
    known_contact_entities = {root(key) for key in known_contact_ids}
    account_roots = {lead.account_id: root(lead.id) for lead in leads if lead.account_id}
    def entity(record):
        return root(record.lead_id) if record.lead_id in parent else account_roots.get(record.account_id)
    future = list(session.scalars(select(Task).where(Task.workspace_id == workspace_id,
        Task.status == 'open', Task.due_at > end)))
    blocked_entities = {entity(t) for t in future}
    blocked_entities.discard(None)
    projection_cache = {}
    def classify(source):
        if source.id not in projection_cache:
            projection_cache[source.id] = read_classification(session, workspace_id, source.lead_id, source.id)
        return projection_cache[source.id]
    due_rows = session.execute(_pipeline_statement(workspace_id, 'calls_actionable', start, end)
                              .order_by(Task.due_at, Task.id)).all()
    all_rows = session.execute(_pipeline_statement(workspace_id, 'all', start, end)
                              .where(~_lead_suppressed(workspace_id)).order_by(Lead.id)).all()
    rows = list(due_rows) + list(all_rows)
    lead_ids = {r.lead_id for r in rows}
    account_ids = {r.account_id for r in rows if r.account_id}
    sources = list(session.scalars(select(Activity).where(Activity.workspace_id == workspace_id,
        Activity.activity_type.in_(('note', 'call', 'email_sent', 'email_received', 'meeting', 'proposal')),
        Activity.occurred_at <= min(end, prepared_at)).order_by(Activity.occurred_at.desc(), Activity.id)))
    superseded = set(session.scalars(select(Activity.supersedes_activity_id).where(
        Activity.workspace_id == workspace_id, Activity.supersedes_activity_id.is_not(None))))
    by_entity = defaultdict(list)
    for source in sources:
        if source.id not in superseded and entity(source) is not None:
            by_entity[entity(source)].append(source)
    contact_cache = {}
    def contacted(source):
        if source.id not in contact_cache:
            contact_cache[source.id] = _contact_activity(source, classify if source.lead_id else None)
        return contact_cache[source.id]
    previous_by_entity = {root(UUID(key)): item for key, item in previous_items.items() if UUID(key) in parent}
    contact_since = {key: [a for a in by_entity[key] if a.occurred_at >= since and contacted(a)]
                     for key in previous_by_entity}
    recycling = {'previous_date': previous_payload.get('work_date'), 'previous_command_id': previous_payload.get('command_id'),
                 'window_start': since.isoformat() if previous else None, 'window_end': prepared_at.isoformat(),
                 'previous_total': len(previous_items), 'contacted_since_generation': sum(bool(v) for v in contact_since.values()),
                 'uncontacted_since_generation': sum(not v for v in contact_since.values()),
                 'missing_current_entity': len(previous_items) - len(previous_by_entity), 'carried': 0}
    # Snapshot batching shares the exact single-profile projection and binding.
    from src.crm.services.commercial_profile import read_profiles
    batch_profiles = read_profiles(session, workspace_id, leads)
    def profile_for(lead_id):
        return batch_profiles[lead_id]
    profiles, contexts = {}, {}
    candidates, excluded = [], Counter()
    due_ids = {r.task_id for r in due_rows}
    due_tasks = {t.id: t for t in session.scalars(select(Task).where(
        Task.workspace_id == workspace_id, Task.id.in_(due_ids)))}
    # Last completed Monday-Friday workweek, observed events not conversations.
    last_friday = work_date - timedelta(days=(work_date.weekday() - 4) % 7 or 7)
    first = datetime.combine(last_friday - timedelta(days=4), time.min, zone).astimezone(UTC)
    last = datetime.combine(last_friday + timedelta(days=1), time.min, zone).astimezone(UTC)
    movement_rows = list(session.scalars(select(Activity).where(Activity.workspace_id == workspace_id,
        Activity.occurred_at >= first, Activity.occurred_at < last,
        Activity.activity_type.in_(('call', 'email_sent')), operational_activity_filter())))
    movement = Counter(a.activity_type for a in movement_rows)
    for row in rows:
        phone = _phone(row.phone)
        name_key = _company(row.company)
        identity = root(row.lead_id)
        history = by_entity[identity]
        if row.lead_id not in contexts:
            contexts[row.lead_id] = _context(history)
            profiles[row.lead_id] = profile_for(row.lead_id)
        context, profile = contexts[row.lead_id], profiles[row.lead_id]
        route, route_block = _next_call(profile, context, history, end)
        if route_block:
            excluded[route_block] += 1
            continue
        prior = previous_by_entity.get(identity)
        recent = contact_since.get(identity, [])
        due = row.task_id in due_ids and row.task_type == 'call'
        task = due_tasks.get(row.task_id) if due else None
        intent = (task.call_intent or {}) if task else {}
        marked_agreed = intent.get('purpose') == 'agreed_callback' and intent.get('agreed_with_client') is True
        agreed = marked_agreed and bool(intent.get('evidence_refs'))
        hold = next((reason for key, reason in _STRATEGY_HOLDS.items() if key in name_key), None)
        if hold or (identity in blocked_entities and not agreed) or not phone:
            excluded['strategy_hold' if hold else 'future_next_step' if phone else 'invalid_phone'] += 1
            continue
        today_contacts = [a for a in history if start <= a.occurred_at <= min(end, prepared_at) and contacted(a)]
        recent_contact = (recent or today_contacts)
        retry_source_ids = {r['activity_id'] for r in route['source_refs']} if route else set()
        explicit_retry = bool(route and route.get('retry_same_day') is True and route['channel'] == 'call' and recent_contact and
            any(str(a.id) in retry_source_ids for a in recent_contact) and
            not any(a.occurred_at > max(b.occurred_at for b in recent_contact if str(b.id) in retry_source_ids)
                    for a in recent_contact))
        if recent_contact and not explicit_retry and (not task or task.due_at <= max(a.occurred_at for a in recent_contact)):
            excluded['contacted_respect_next_step'] += 1
            continue
        prior_task = (prior.get('task') or {}).get('id') if prior else None
        if prior_task and prior_task not in {str(key) for key in due_ids} and not due:
            excluded['previous_task_no_longer_actionable'] += 1
            continue
        carry = bool(prior and not recent and (not prior_task or prior_task == str(row.task_id)))
        if row.lead_id not in contexts:
            contexts[row.lead_id] = _context(history)
            profiles[row.lead_id] = profile_for(row.lead_id)
        context, profile = contexts[row.lead_id], profiles[row.lead_id]
        # Restrict CALL selection only: retain original notes/tasks and the
        # independent email cadence. An old task, fit or recycling is not consent.
        if context['call_restrictions'] and not route:
            excluded['original_call_channel_restriction'] += 1
            continue
        fit_field = profile.get('fields', {}).get('fit', {})
        fit = (fit_field.get('value') or {}).get('status') if fit_field.get('current_entity') else None
        if (context['refusals'] or context['holds']) and not route or fit == 'no_fit':
            excluded['refusal_or_no_fit'] += 1
            continue
        due = row.task_id in due_ids and row.task_type == 'call'
        intent = (due_tasks[row.task_id].call_intent or {}) if due else {}
        marked_agreed = intent.get('purpose') == 'agreed_callback' and intent.get('agreed_with_client') is True
        agreed = marked_agreed and bool(intent.get('evidence_refs'))
        if due:
            # Internal backlog is not a client commitment and must not crowd
            # out active opportunities. Keep its canonical task/UI cohort.
            rank = 0 if agreed else 1 if context['interest'] else 3 if intent.get('purpose') == 'internal_preparation' else 2
            cohort, reason = 'calls_actionable', ('Callback com acordo e referências no CRM: ' if agreed else 'Tarefa CRM devida, acordo por confirmar na fonte: ') + row.task_title
            objective = 'Executar a tarefa existente, confirmar contexto e registar resultado'
            order = row.task_due_at.isoformat()
        elif explicit_retry:
            cohort, rank, reason = 'follow_up', 2, route['reason']
            objective, order = 'Respeitar o próximo telefonema indicado na fonte atual', route.get('not_before') or ''
        elif context['interest']:
            cohort, rank, reason = 'hot_context', 1, 'Interesse explícito na fonte original; não inferido de etapa ou telefone'
            objective, order = 'Validar necessidade e acordar próximo passo, sem presumir compra', ''
        else:
            external = [a for a in history if contacted(a)]
            # Preparation notes never count as contact; historical imported calls
            # and unclassified personal notes must not become a false first touch.
            uncertain_history = any(_uncertain_first_touch(a) for a in history)
            untouched = (not external and not uncertain_history and
                identity not in known_contact_entities and row.stage == 'new')
            if untouched:
                cohort, rank, order = 'fresh_fit', 4, ''
                reason = 'New pipeline — lead ainda não contactada'
                objective = 'Primeira prospeção; confirmar contexto sem presumir interesse ou fit'
            elif fit == 'fit':
                cohort, rank, order = 'follow_up', 2, external[0].occurred_at.isoformat() if external else ''
                reason = 'Fit revisto com evidência; seguimento de movimento real registado'
                objective = 'Confirmar processo manual e benefício económico; não prometer poupança sem medir'
            else:
                excluded['no_supported_fit_or_action'] += 1
                continue
        if carry:
            rank = min(rank, 1.5)
            # Keep UI task cohorts; recycling is an internal selection dimension.
            if cohort == 'fresh_fit': cohort = 'carryover'
        item = _to_item(row).model_dump(mode='json')
        financial = _financial(profile)
        source_refs = ([r for r in context['sources'] if r['activity_id'] in {s['activity_id'] for s in route['source_refs']}]
                       if route else context['interest'] if context['interest'] else context['sources'][:1])
        provenance = dict(prior.get('selection_provenance') or {}) if carry else {}
        provenance.setdefault('origin_date', previous_payload.get('work_date') if carry else work_date.isoformat())
        if not provenance.get('origin_command_id'):
            provenance['origin_command_id'] = previous_payload.get('command_id') if carry else None
        provenance['previous_command_id'] = previous_payload.get('command_id') if prior else None
        provenance['recycled_from_date'] = previous_payload.get('work_date') if carry else None
        provenance['contact_activity_ids_since_generation'] = [str(a.id) for a in recent]
        candidates.append(dict(item, cohort=cohort, reason=reason, objective=objective, carried_over=carry,
            selection_provenance=provenance,
            source_refs=source_refs, next_contact=route, commercial_evidence=profile['fields'], financial_evidence=financial,
            history_status='confirmed_uncontacted' if reason == 'New pipeline — lead ainda não contactada' else 'unknown', agreed_with_client=agreed, agreement_status='referenced_canonical_intent' if agreed else 'unverified_legacy_intent' if marked_agreed else 'not_asserted', call_intent=intent if due else None, estimated_minutes=5,
            selection_group='due_callback' if agreed else 'explicit_interest' if context['interest'] else 'due_internal' if due else cohort,
            _identity=str(identity), _phone=phone, _rank=rank, _order=order))
    entries, available, duplicates = select_contacts(candidates, target_contacts)
    recycling['carried'] = sum(i['carried_over'] for i in entries)
    recycling['selected_existing_next_step'] = sum(bool(i['selection_provenance']['contact_activity_ids_since_generation']) for i in entries)
    recycling['added_other_contacts'] = len(entries) - recycling['carried']
    recycling['not_carried'] = recycling['uncontacted_since_generation'] - recycling['carried']
    # Read existing source-bound Jev results only for the selected sources. Never
    # invoke a classifier or infer interest from attendance/role/callback dimensions.
    sources_by_id = {str(a.id): a for a in sources}
    for item in entries:
        item['jev_context'] = []
        if not enrich:
            continue
        for ref in item['source_refs']:
            source = sources_by_id.get(ref['activity_id'])
            if source and source.lead_id and source.activity_type in ('note', 'call'):
                item['jev_context'].append(classify(source))
    blockers = ['Lista de preparação, não meta de chamadas nem prova de conversa. Validar fonte original antes de contactar.']
    if len(entries) < target_contacts:
        blockers.append(f'Contactos elegíveis insuficientes: {len(entries)} de {target_contacts}; preparação incompleta, owner Main/Sales.')
    return dict(schema_version=2, work_date=work_date.isoformat(), date=work_date.isoformat(),
        timezone=zone_name, capacity_minutes=capacity_minutes, target_contacts=target_contacts, target_first_answered=10,
        items=entries, total=len(entries), available_counterparties=available,
        deduplicated_candidates=duplicates, excluded_candidates=dict(excluded), source_status='available' if len(entries) == target_contacts else 'partial',
        recycling=recycling, selection_policy='due_callbacks_then_opportunities_then_uncontacted_carryover_then_other_eligible',
        recent_movement={'start_date': (last_friday - timedelta(days=4)).isoformat(), 'end_date': last_friday.isoformat(),
            'calls': movement['call'], 'emails_sent': movement['email_sent'],
            'meaning': 'operational events, not qualified conversations; follow-up precedes fresh fit, no fixed quotas'},
        blockers=blockers, new_tasks_created=0, calendar_events_created=0,
        generated_at=datetime.now(UTC).isoformat())


def prepare_call_day(session, principal, body):
    from fastapi import HTTPException
    session.scalar(select(Workspace).where(Workspace.id == principal.workspace_id).with_for_update())
    if body.reconcile:
        from src.crm.services.call_day_reconcile import reconcile_call_day
        return reconcile_call_day(session, principal, body)
    material = body.model_dump(mode='json')
    for key in ('reconcile', 'context_sha256', 'restore_command_id', 'replenish'):
        material.pop(key, None)  # Keep historical preparation replay hashes.
    # Preserve the hash of pre-target clients for exact replay after upgrade.
    if 'target_contacts' not in body.model_fields_set:
        material.pop('target_contacts', None)
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    existing = session.get(CallDayPlan, (principal.workspace_id, body.work_date))
    if existing:
        if existing.command_id != body.command_id or existing.request_hash != digest or existing.actor_id != principal.actor_id:
            from src.crm.services.call_day_reconcile import original_replay
            replay = original_replay(session, principal, body, digest)
            if replay is not None:
                return replay
            raise HTTPException(409, 'Command conflict')
        return dict(existing.payload, replayed=True)
    if body.expected_version != 0:
        raise HTTPException(409, 'Command conflict')
    payload = build_call_day(session, principal.workspace_id, body.work_date, body.capacity_minutes, body.target_contacts)
    payload.update(command_id=str(body.command_id), version=1)
    for item in payload['items']:
        if not item['selection_provenance'].get('origin_command_id'):
            item['selection_provenance']['origin_command_id'] = str(body.command_id)
    session.add(CallDayPlan(workspace_id=principal.workspace_id, work_date=body.work_date,
        command_id=body.command_id, actor_id=principal.actor_id, request_hash=digest, version=1, payload=payload))
    session.flush()
    return dict(payload, replayed=False)
