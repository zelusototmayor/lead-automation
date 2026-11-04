"""Persisted conditional forecasts, never sender obligations or send authority.

GET evaluates fresh fences without writing. Explicit reconciliation persists the
same lifecycle view using a fresh context CAS; SENT/reviews/credits are untouched.
"""
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select

from src.crm.persistence.models import AgentWork, Task
from src.crm.services.followup_policy import CADENCE_VERSION, MAX_STAGE, POLICY_ID, due_at
from src.crm.services.followup_review import FollowupError, timestamp, utc

KEY = 'followup_projection_v1'


def lead_work(session, workspace, lead_id, *, lock=False):
    statement = select(AgentWork).where(
        AgentWork.workspace_id == workspace, AgentWork.lead_id == lead_id
    ).order_by(AgentWork.id)
    if lock:
        statement = statement.with_for_update()
    return list(session.scalars(statement))


def records(session, workspace, lead_id, mailbox, *, lock=False):
    return [row for row in lead_work(session, workspace, lead_id, lock=lock)
            if row.payload.get(KEY, {}).get('mailbox') == mailbox]


def historical_rows(values, mailbox):
    # Optional anchor metadata on other work kinds is not historical evidence.
    # Keep incomplete sent-review anchors so POST can report individual residuals.
    output = []
    for row in values:
        if row.kind != 'sent_review' or KEY in row.payload or 'followup_anchor_v1' not in row.payload:
            continue
        state = row.payload['followup_anchor_v1']
        if not isinstance(state, dict) or state.get('mailbox') in (None, mailbox):
            output.append(row)
    return output


def approved_history_item(work, decision):
    """Historical semantic approval, not a renewed send grant or live lease."""
    from src.crm.services.followup_context import digest
    requests = work.payload.get('followup_anchor_v1', work.payload.get('followup_v1', {})).get('review', {}).get('requests', [])
    matches = [item for item in requests
               if item['package']['request_id'] == decision['review_request_id']]
    if len(matches) != 1:
        raise FollowupError('historical_review_missing_or_ambiguous')
    item = matches[0]
    package, receipt = item['package'], item.get('receipt') or {}
    from uuid import UUID, uuid5
    if (item['status'] != 'approved' or not item.get('consumed_at')
            or digest(package) != item['request_hash'] or item['request_hash'] != decision['request_hash']
            or receipt.get('id') != decision['reviewer_receipt_id']
            or receipt.get('authenticated_actor_id') != package['reviewer_actor_id']
            or UUID(package['reviewer_actor_id']) == uuid5(work.workspace_id, 'head-of-sales')
            or receipt.get('runner_outcome') != 'verdict'
            or (receipt.get('verdict') or {}).get('decision') != 'APPROVE'
            or package['work_id'] != str(work.id) or package['workspace_id'] != str(work.workspace_id)):
        raise FollowupError('historical_review_binding_mismatch')
    return package


def historical_receipt(work, values, context):
    """Only exact canonical classification / own-SENT receipts can seed facts."""
    import hashlib
    from src.crm.connectors.followup_email import decode_payload, receipt_matches
    from src.crm.services import followup_service as service
    from src.crm.services.followup_context import digest
    from src.crm.services.followup_policy import obligation_key
    state = work.payload['followup_anchor_v1']
    klass, decision = state['classification'], state.get('decision') or {}
    if klass not in {'intro', 'fu1', 'fu2', 'fu3', 'fu4', 'fu5'}:
        raise FollowupError('historical_classification_unknown')
    if (work.kind != 'sent_review' or work.status != 'completed'
            or state['schema'] != 'ordinary-followup-anchor/v1' or state['purpose'] != 'anchor_classification'
            or state['lead_id'] != str(work.lead_id)):
        raise FollowupError('historical_anchor_not_canonical')
    anchor = service.verified_anchor(context, state['provider_message_id'])
    if anchor['thread_id'] != state['thread_id'] or digest(anchor) != decision['observation_version']:
        raise FollowupError('historical_observation_changed')
    stage = 0 if klass == 'intro' else int(klass[-1])
    if decision['purpose'] == 'anchor_classification':
        package = approved_history_item(work, decision)
        if (package['purpose'] != 'anchor_classification' or package['policy_id'] != POLICY_ID
                or package['mailbox'] != state['mailbox']
                or package['lead_id'] != str(work.lead_id) or package['thread_id'] != state['thread_id']
                or package['provider_message_id'] != anchor['id']
                or package['observation_version'] != decision['observation_version']
                or package['proposal']['classification'] != klass
                or (work.result or {}).get('classification') != klass
                or (work.result or {}).get('reviewer_receipt_id') != decision['reviewer_receipt_id']
                or work.result_hash != digest(work.result)):
            raise FollowupError('historical_classification_binding_mismatch')
        evidence = {'purpose': decision['purpose'], 'reviewer_receipt_id': decision['reviewer_receipt_id']}
    elif decision['purpose'] == 'own_sent_receipt':
        sources = [row for row in values if str(row.id) == decision['source_work_id']]
        if len(sources) != 1:
            raise FollowupError('historical_source_work_missing')
        source = sources[0]
        sent_state = source.payload['followup_v1']
        obligation, intent, result = sent_state['obligation'], sent_state['intent'], source.result or {}
        package = approved_history_item(source, sent_state['decision'])
        raw = decode_payload(package['payload_raw_b64'])
        task = next((t for t in context['crm']['tasks_all_statuses'] if t['id'] == str(source.task_id)), None)
        activity = next((a for a in context['crm']['activities'] if a['id'] == result.get('activity_id')), None)
        key = obligation_key(source.workspace_id, state['mailbox'], source.lead_id,
                             state['thread_id'], stage, obligation['anchor_message_id'])
        from src.crm.services.followup_policy import task_id
        if (source.kind != 'followup_due' or source.status != 'completed' or stage == 0
                or source.task_id != task_id(source.workspace_id, key)
                or not task or task['workspace_id'] != str(source.workspace_id)
                or task['lead_id'] != str(work.lead_id) or task['source_rule'] != POLICY_ID
                or task['task_type'] != 'email'
                or package['expected_task_version'] != sent_state['task_version']
                or source.payload['task_version'] != sent_state['task_version']
                or package['expected_lead_version'] != sent_state['expected_lead_version']
                or not activity or activity['lead_id'] != str(work.lead_id)
                or activity['activity_type'] != 'email_sent' or activity['occurred_at'] != anchor['sent_at']
                or sent_state['policy_id'] != POLICY_ID or source.source_key != key or obligation['key'] != key
                or obligation['lead_id'] != str(work.lead_id) or obligation['mailbox'] != state['mailbox']
                or obligation['thread_id'] != state['thread_id'] or obligation['fu_stage'] != stage
                or package.get('purpose') is not None or package['obligation_key'] != key
                or package['mailbox'] != state['mailbox'] or package['context']['obligation'] != obligation
                or intent['state'] != 'crm_applied' or intent['intent_id'] != decision['intent_id']
                or intent['provider_message_id'] != anchor['id'] or intent['sent_at'] != anchor['sent_at']
                or result.get('intent_state') != 'crm_applied' or result.get('provider_message_id') != anchor['id']
                or result.get('work_id') != str(source.id) or source.result_hash != digest(result)
                or hashlib.sha256(raw).hexdigest() != intent['payload_sha256']
                or package['payload_sha256'] != intent['payload_sha256']
                or not receipt_matches(anchor, raw, obligation, timestamp(intent['reserved_at']))):
            raise FollowupError('historical_own_sent_binding_mismatch')
        evidence = {'purpose': decision['purpose'], 'source_work_id': str(source.id), 'intent_id': intent['intent_id']}
    else:
        raise FollowupError('historical_receipt_missing')
    return stage, anchor, evidence


def bootstrap(session, principal, values, context):
    """CAS caller holds company/Task/all lead-work locks. Never creates Tasks."""
    from src.crm.services.followup_review import audit
    candidates, residuals, groups = [], [], {}
    for work in historical_rows(values, context['provider']['mailbox']):
        try:
            stage, anchor, evidence = historical_receipt(work, values, context)
            # Assemble every per-work field before adding its facts to a group,
            # and before any payload/audit mutation. A partial anchor cannot veto
            # a separate valid receipt or leak facts into its projection.
            value = {'schema': KEY, 'cadence_version': CADENCE_VERSION,
                     'mailbox': context['provider']['mailbox'], 'lead_id': str(work.lead_id),
                     'thread_id': anchor['thread_id'],
                     'assessment': deepcopy(work.payload['followup_anchor_v1']['assessment']),
                     'send_authority': False,
                     'bootstrap': {'work_id': str(work.id), 'context_hash': context['context_hash'], **evidence}}
            if not isinstance(value['assessment'], dict):
                raise FollowupError('historical_receipt_incomplete')
        except FollowupError as exc:
            residuals.append({'work_id': str(work.id), 'reason_code': exc.code})
            continue
        except (KeyError, TypeError, ValueError, AttributeError):
            residuals.append({'work_id': str(work.id), 'reason_code': 'historical_receipt_incomplete'})
            continue
        candidates.append((work, stage, anchor, evidence, value))
        groups.setdefault(anchor['thread_id'], []).append(candidates[-1])
    created = []
    for thread, group in groups.items():
        actual, conflict = {}, False
        facts = [r.payload[KEY]['actual_sent'] for r in values
                 if r.payload.get(KEY, {}).get('mailbox') == context['provider']['mailbox']
                 and r.payload[KEY]['thread_id'] == thread]
        facts += [{str(stage): {'message_id': anchor['id'], 'sent_at': anchor['sent_at']}}
                  for _, stage, anchor, _, _ in group]
        for fact in facts:
            for stage, receipt in fact.items():
                if stage in actual and actual[stage] != receipt:
                    conflict = True
                actual[stage] = receipt
        ordered = [timestamp(actual[k]['sent_at']) for k in sorted(actual, key=int)]
        if conflict or ordered != sorted(ordered):
            residuals += [{'work_id': str(w.id), 'reason_code': 'historical_stage_mapping_ambiguous'} for w, *_ in group]
            continue
        latest = max(map(int, actual))
        for work, stage, anchor, evidence, value in group:
            value.update({'actual_sent': deepcopy(actual), 'latest_stage': latest,
                          'anchor_message_id': actual[str(latest)]['message_id'], 'stages': stages_from_actual(actual)})
            work.payload = deepcopy(work.payload) | {KEY: value}
            audit(session, work, principal.actor_id, 'projection_bootstrapped',
                  {'context_hash': context['context_hash'], 'stage': stage, 'provider_message_id': anchor['id'], **evidence})
            created.append(str(work.id))
    session.flush()
    return {'created_work_ids': created, 'residuals': residuals}


def stages_from_actual(actual):
    latest = max(map(int, actual))
    base = timestamp(actual[str(latest)]['sent_at'])
    stages = []
    for number in range(1, MAX_STAGE + 1):
        receipt = actual.get(str(number))
        if receipt:
            stages.append({'stage': number, 'status': 'sent', 'actual_sent_at': receipt['sent_at'],
                           'provider_message_id': receipt['message_id'], 'projected_due_at': None})
        elif number <= latest:
            stages.append({'stage': number, 'status': 'unknown_history', 'actual_sent_at': None,
                           'provider_message_id': None, 'projected_due_at': None})
        else:
            base = due_at(base, number)
            stages.append({'stage': number, 'status': 'scheduled' if number == latest + 1 else 'forecast',
                           'actual_sent_at': None, 'provider_message_id': None,
                           'projected_due_at': utc(base),
                           'basis': 'actual_preceding_sent' if number == latest + 1 else 'conditional_forecast'})
    return stages


def persist(session, work, context, anchor, classification, assessment):
    if classification not in {'intro', 'fu1', 'fu2', 'fu3', 'fu4', 'fu5'}:
        return
    mailbox = context['provider']['mailbox']
    stage = 0 if classification == 'intro' else int(classification[-1])
    previous = [r.payload[KEY] for r in records(session, work.workspace_id, work.lead_id, mailbox)
                if r.payload[KEY]['thread_id'] == anchor['thread_id']]
    actual = {}
    for value in previous:
        actual.update(value['actual_sent'])
    actual[str(stage)] = {'message_id': anchor['id'], 'sent_at': anchor['sent_at']}
    latest = max(map(int, actual))
    value = {'schema': KEY, 'cadence_version': CADENCE_VERSION, 'mailbox': mailbox,
             'lead_id': str(work.lead_id), 'thread_id': anchor['thread_id'],
             'actual_sent': actual, 'latest_stage': latest, 'anchor_message_id': actual[str(latest)]['message_id'],
             'stages': stages_from_actual(actual), 'assessment': deepcopy(assessment), 'send_authority': False}
    work.payload = deepcopy(work.payload) | {KEY: value}
    session.flush()


def latest_records(values):
    by_thread = {}
    for row in values:
        value = row.payload[KEY]
        old = by_thread.get(value['thread_id'])
        rank = (value['latest_stage'], timestamp(value['actual_sent'][str(value['latest_stage'])]['sent_at']),
                len(value['actual_sent']), str(row.id))
        if old is None or rank > old[0]:
            by_thread[value['thread_id']] = (rank, row)
    return [row for _, row in by_thread.values()]


def evaluate(session, principal, persisted, context, now):
    from src.crm.services import followup_service as service
    value = deepcopy(persisted)
    # Rebuild only forecasts; a prior persisted hold cannot become a fake receipt.
    value['stages'] = stages_from_actual(value['actual_sent'])
    for receipt in value['actual_sent'].values():
        confirmed = service.verified_anchor(context, receipt['message_id'])
        if confirmed['sent_at'] != receipt['sent_at']:
            raise FollowupError('projection_receipt_changed')
    anchor = service.verified_anchor(context, value['anchor_message_id'])
    next_stage = value['latest_stage'] + 1
    works = [w for w in session.scalars(select(AgentWork).where(
        AgentWork.workspace_id == principal.workspace_id, AgentWork.lead_id == value['lead_id'],
        AgentWork.kind == 'followup_due'))
        if w.payload.get('followup_v1', {}).get('obligation', {}).get('anchor_message_id') == anchor['id']
        and w.payload['followup_v1']['obligation'].get('mailbox') == value['mailbox']
        and w.payload['followup_v1']['obligation'].get('thread_id') == value['thread_id']
        and w.payload['followup_v1']['obligation'].get('fu_stage') == next_stage]
    work = works[0] if len(works) == 1 else None
    task = session.get(Task, work.task_id) if work and work.task_id else None
    current = service.assessment(context, anchor, own_task_id=str(task.id) if task else None)

    def hold(reason, disposition='deferred', not_before=None):
        return {'disposition': disposition, 'reason_code': reason,
                'not_before': not_before, 'evidence_refs': []}

    if next_stage > MAX_STAGE:
        current = hold('sequence_finished', 'sequence_finished')
    elif current['disposition'] == 'eligible':
        state = work.payload['followup_v1'] if work else {}
        if not work or not task or task.workspace_id != principal.workspace_id or task.lead_id != work.lead_id:
            current = hold('canonical_obligation_missing_or_ambiguous')
        elif task.status == 'cancelled':
            current = hold('canonical_task_cancelled', 'suppressed')
        elif task.status != 'open':
            current = hold('canonical_task_receipt_reconciliation_required')
        elif (task.source_rule != POLICY_ID or task.task_type != 'email'
              or task.version != state.get('task_version')
              or task.version != work.payload.get('task_version')
              or context['crm']['lead']['version'] != state.get('expected_lead_version')):
            current = hold('task_or_lead_changed')
        elif work.status not in {'queued', 'running'} or state.get('intent') is not None:
            current = hold('canonical_work_requires_reconciliation')
        else:
            item = value['stages'][next_stage - 1]
            policy_due = timestamp(item['projected_due_at'])
            effective_due = max(policy_due, task.due_at, work.available_at,
                                timestamp(current['not_before']) if current.get('not_before') else policy_due)
            if effective_due > policy_due:
                item['projected_due_at'] = utc(effective_due)
                item['basis'] = 'canonical_not_before'
                base = effective_due
                for future in value['stages'][next_stage:]:
                    base = due_at(base, future['stage'])
                    future['projected_due_at'] = utc(base)
            if effective_due > now:
                current = hold('canonical_not_yet_due', 'scheduled', utc(effective_due))
    for item in value['stages']:
        if item['status'] in {'sent', 'unknown_history'}:
            continue
        if current['disposition'] not in {'eligible', 'scheduled'}:
            item['status'] = 'cancelled' if current['disposition'] in {'sequence_finished', 'suppressed'} else 'deferred'
            item['reason_code'] = current['reason_code']
        elif item['stage'] == next_stage:
            item['status'] = 'due' if current['disposition'] == 'eligible' else 'scheduled'
    value['effective_assessment'] = current
    value['evaluated_at'] = utc(now)
    value['context_hash'] = context['context_hash']
    value['next_eligible_stage'] = next_stage if current['disposition'] == 'eligible' and next_stage <= MAX_STAGE else None
    return value


def projection_context(session, principal, lead_id, mailbox, provider, values):
    from src.crm.services import followup_service as service
    service.mailbox_scope(principal, mailbox)
    threads = {r.payload[KEY]['thread_id'] for r in values if r.payload.get(KEY, {}).get('mailbox') == mailbox}
    threads.update(r.payload['followup_anchor_v1']['thread_id'] for r in historical_rows(values, mailbox)
                   if isinstance(r.payload['followup_anchor_v1'], dict)
                   and isinstance(r.payload['followup_anchor_v1'].get('thread_id'), str))
    return service.current_context(session, principal, lead_id, mailbox, provider, thread_ids=tuple(threads))


def query(session, principal, lead_id, mailbox, provider, *, now=None):
    all_work = lead_work(session, principal.workspace_id, lead_id)
    values = latest_records([r for r in all_work if r.payload.get(KEY, {}).get('mailbox') == mailbox])
    context = projection_context(session, principal, lead_id, mailbox, provider, all_work)
    output = [evaluate(session, principal, row.payload[KEY], context, now or datetime.now(UTC)) for row in values]
    return {'items': output, 'summary': sequence_summary(output, fresh=True),
            'send_authority': False, 'forecast_is_sent': False,
            'context_hash': context['context_hash'], 'expected_lead_version': context['crm']['lead']['version']}


def reconcile(session, principal, lead_id, mailbox, provider, body, *, now=None):
    from src.crm.services import followup_service as service
    service.mailbox_scope(principal, mailbox)
    service._locked_company_lead(session, principal.workspace_id, lead_id)
    # Same row/Task locks as send preparation, before the fresh source CAS.
    service._locked_rows(session, select(Task).where(Task.workspace_id == principal.workspace_id,
                                                    Task.lead_id == lead_id))
    all_work = lead_work(session, principal.workspace_id, lead_id, lock=True)
    context = projection_context(session, principal, lead_id, mailbox, provider, all_work)
    if (body.context_hash != context['context_hash']
            or body.expected_lead_version != context['crm']['lead']['version']):
        raise FollowupError('context_changed')
    hydrated = bootstrap(session, principal, all_work, context)
    values = latest_records([r for r in all_work if r.payload.get(KEY, {}).get('mailbox') == mailbox])
    output = []
    for row in values:
        value = evaluate(session, principal, row.payload[KEY], context, now or datetime.now(UTC))
        row.payload = deepcopy(row.payload) | {KEY: value}
        output.append(value)
    session.flush()
    return {'items': output, 'summary': sequence_summary(output, fresh=True),
            'persisted': True, 'send_authority': False, 'forecast_is_sent': False,
            'context_hash': context['context_hash'], 'expected_lead_version': context['crm']['lead']['version'],
            'bootstrap': hydrated}


def sequence_summary(values, *, fresh=False):
    """UI projection from canonical SENT phases, never count-1 or send authority.

    A cached CRM view does not certify current Gmail/context or full commercial
    SENT coverage. Conflicting identities/classes have no invented phase.
    """
    facts, entries = {}, []
    for value in values:
        for stage, receipt in value['actual_sent'].items():
            key = (value['mailbox'], receipt['message_id'])
            fact = (int(stage), receipt['sent_at'])
            if key in facts and facts[key] != fact:
                return {'label': None, 'reason_code': 'canonical_phase_conflict', 'send_authority': False}
            facts[key] = fact
            entries.append((timestamp(receipt['sent_at']), int(stage), value, receipt))
    if not entries:
        return None
    _, stage, value, receipt = max(entries, key=lambda item: (item[0], item[1]))
    # Separate live threads must not be silently collapsed into one obligation.
    threads = {(v['mailbox'], v['thread_id']) for v in values}
    if len(threads) > 1:
        return {'label': None, 'reason_code': 'multiple_threads_require_reconciliation',
                'classified_sequence_sent_count': len(facts), 'commercial_sent_total': None,
                'multiple_threads_require_reconciliation': True, 'send_authority': False}
    current = value.get('effective_assessment', value.get('assessment', {}))
    closed = stage == MAX_STAGE
    upcoming = next((s for s in value['stages'] if s['stage'] == stage + 1), None)
    actionable = fresh and len(threads) == 1
    reason = ('sequence_finished' if closed else current.get('reason_code')) if actionable else 'fresh_reconciliation_required'
    return {'label': 'Fechado' if closed else 'Email inicial enviado' if stage == 0 else f'FU{stage}',
            'last_phase': 'intro' if stage == 0 else f'fu{stage}',
            'last_sent_at': receipt['sent_at'], 'last_provider_message_id': receipt['message_id'],
            'classified_sequence_sent_count': len(facts), 'commercial_sent_total': None,
            'coverage': 'canonical_sequence_receipts_only_not_all_commercial_sent',
            'next_email': None if closed else f'FU{stage + 1}',
            'next_due_at': upcoming.get('projected_due_at') if actionable and upcoming else None,
            'next_action': ('Sequência esgotada; preservar estágio comercial' if closed else
                            current.get('next_action') or f'Preparar FU{stage + 1} contextual após revisão' if actionable else 'Reconciliar contexto atual'),
            'reason_code': reason, 'sequence_closed': closed,
            'closure_scope': 'sequence_only' if closed else None,
            'commercial_stage_changed': False, 'fresh_context': fresh,
            'multiple_threads_require_reconciliation': len(threads) > 1, 'send_authority': False}


def stored_summaries(session, workspace, lead_ids, mailbox):
    """One bounded-page CRM read; no Gmail scans, writes or new obligations."""
    grouped = {identifier: [] for identifier in lead_ids}
    if not grouped:
        return {}
    for row in session.scalars(select(AgentWork).where(
            AgentWork.workspace_id == workspace, AgentWork.lead_id.in_(grouped),
            AgentWork.kind == 'sent_review')):
        value = row.payload.get(KEY)
        if value and value.get('mailbox') == mailbox:
            grouped[row.lead_id].append(row)
    return {identifier: sequence_summary([row.payload[KEY] for row in latest_records(rows)])
            for identifier, rows in grouped.items()}
