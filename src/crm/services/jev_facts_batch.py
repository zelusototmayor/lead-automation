"""Resumable Sales consumer of canonical APIs. No DB, provider key or leases.

Input is a reviewed source/work manifest, not a bulk model reclassification of
all leads. CLI uncertainty requires explicit reconciliation. The local consumer
may request bounded same-work reconciliation through the canonical receipt fence,
never direct provider retries. Every accepted effect is read back from its source.
"""
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import re
from uuid import UUID

from src.crm.services.jev_private_files import private_write
from src.crm.services.jev_note_policy import digest


def _save_checkpoint(path, state):
    private_write(path, json.dumps(state))
    # Atomic rename plus fsync(file) alone can lose the directory entry on a
    # power loss. Persist that entry before permitting any remote effect.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_entries(entries, limit):
    if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(entries, list):
        raise ValueError('invalid_batch_limit')
    for item in entries:
        if set(item) != {'work_id', 'lead_id', 'activity_id', 'source_digest'}:
            raise ValueError('manifest_shape')
        for key in ('work_id', 'lead_id', 'activity_id'):
            if str(UUID(item[key])) != item[key]:
                raise ValueError('noncanonical_identity')
        if not re.fullmatch('[0-9a-f]{64}', item['source_digest']):
            raise ValueError('source_digest_invalid')
    if len({i['work_id'] for i in entries}) != len(entries):
        raise ValueError('duplicate_work')


def _work_preserved(before, after, *, personal=False):
    def note_source(work):
        source = work['context'].get('note_source')
        if personal and isinstance(source,dict):
            return {k:v for k,v in source.items() if k not in ('history','newer_context')}
        return source
    return (all(after.get(k) == before.get(k) for k in (
        'id', 'kind', 'lead_id', 'task_id', 'status', 'attempt', 'result',
        'result_hash', 'payload', 'available_at', 'error', 'updated_at'))
        and note_source(after) == note_source(before)
        and all(after['context'].get(k) == before['context'].get(k)
            for k in ('lead_version', 'suppressed', 'tasks', 'open_tasks')))


def _verify_advisory(client, entry, before, receipt, item):
    applied = receipt.get('application') or {}
    if applied.get('status') != 'applied':
        item['status'] = 'receipt_conflict'
        return
    readback = client.call('GET',
        f"/api/v1/agent/leads/{entry['lead_id']}/notes/{entry['activity_id']}/jev-hints")
    after = client.call('GET', f"/api/v1/agent/work/{entry['work_id']}")
    hints = applied.get('hints') or {}
    if (not _work_preserved(before, after)
            or readback.get('activity_id') != entry['activity_id']
            or readback.get('source_status') != 'current'
            or readback.get('source_digest') != entry['source_digest']
            or readback.get('fact_authority') is not False
            or set(hints) != {'interest'}
            or applied.get('sales_context', {}).get('source_work_unchanged') is not True
            or any(readback.get('hints', {}).get(k) != hint
                or readback.get('receipt_ids', {}).get(k) != receipt['receipt_id']
                or hint.get('fact_authority') is not False
                or hint.get('evidence_refs') != [{'activity_id': entry['activity_id'],
                    'source_digest': entry['source_digest'], 'support': 'unverified_hint'}]
                for k, hint in hints.items())):
        item['status'] = 'readback_conflict'
    else:
        item.update(status='verified' if hints['interest']['value'] in ('yes', 'no') else 'abstained',
            policy='note_interest', diff_digest=applied['diff_digest'], hints_digest=digest(readback))
        return readback


def _reviewed_adjudication(entry, before, applied, facts):
    """Recognize one exact server-audited withdrawal, never ignore a conflict."""
    if facts.get('projection_status') != 'reviewed_adjudication' or facts.get('conflict') is not False:
        return None
    try:
        rows = {p['audit_id']: p for p in facts['provenance']}
        if len(rows) != len(facts['provenance']):
            return None
        target = rows[applied['audit_id']]
        reviews = [r for r in facts['adjudications'] if r['target_audit_id'] == applied['audit_id']]
        if len(reviews) != 1:
            return None
        review = reviews[0]; support = rows[review['supporting_audit_id']]
        if (target['facts'] != applied['facts'] or target['jev'] != applied['jev']
                or target['withdrawn_fields'] != ['answer_kind']
                or target['facts'].get('answer_kind') != 'no_answer'
                or support['facts'].get('answer_kind') != 'human_counterparty'
                or support.get('withdrawn_fields') not in (None, [])
                or support['audit_id'] == target['audit_id']
                or any(p.get('source_digest') != entry['source_digest'] or p.get('work_id') != entry['work_id']
                       or not re.fullmatch('[0-9a-f]{64}',p.get('audit_payload_digest','')) for p in (target,support))
                or target['jev'].get('source_digest') != entry['source_digest']
                or review['source_digest'] != entry['source_digest']
                or review['status'] != 'reviewed_adjudication' or review['inference_performed'] is not False
                or review['field'] != 'answer_kind' or review['reason_code'] != 'semantic_model_error'
                or not re.fullmatch('[0-9a-f]{64}',review.get('audit_payload_digest',''))
                or str(UUID(review['audit_id'])) != review['audit_id']
                or not review['quote'].strip() or review['quote'] not in before['context']['note_source']['summary']
                or not review['review_ref'].strip()):
            return None
        candidates=[]
        for row in rows.values():
            if row.get('work_id') != entry['work_id'] or row.get('source_digest') != entry['source_digest']:
                return None
            # Only the one exact target field may differ from its original
            # application. Additional unresolved or unrelated removals fail.
            if row['audit_id'] != target['audit_id'] and row.get('withdrawn_fields') not in (None, []):
                return None
            candidates.append({k:v for k,v in row['facts'].items()
                if not (row['audit_id'] == target['audit_id'] and k == 'answer_kind')})
        # Client-side comparison only: finite v1 fact fields, exact types and
        # agreement. Keep this leaf consumer usable without server/ORM imports.
        enums = {'answer_kind': {'human_counterparty','no_answer','ivr','voicemail','wrong_number'},
                 'interlocutor_role': {'reception','decision_maker','other'}}
        merged = {}
        for candidate in candidates:
            if not isinstance(candidate,dict) or not set(candidate) <= set(enums) | {'useful','decision_maker','repeat_reason'}:
                return None
            for field,value in candidate.items():
                if value is None or value == 'unknown':
                    continue
                valid = (type(value) is bool if field in ('useful','decision_maker') else
                    isinstance(value,str) and value in enums[field] if field in enums else
                    isinstance(value,str) and len(value) <= 500)
                if not valid or field in merged and (type(merged[field]) is not type(value) or merged[field] != value):
                    return None
                merged[field] = value
        derived = facts['derived_facts']
        if (not isinstance(derived, dict) or set(merged) != set(derived)
                or any(type(derived[key]) is not type(value) or derived[key] != value
                       for key, value in merged.items())):
            return None
        return {'audit_id':review['audit_id'],'target_audit_id':target['audit_id'],
                'supporting_audit_id':support['audit_id'],'source_digest':entry['source_digest'],
                'audit_payload_digest':review['audit_payload_digest'],'reason_code':'semantic_model_error'}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def run_batch(client, entries, checkpoint, *, limit=1, advisory=False, reconcile=False, personal=False):
    validate_entries(entries, limit)
    if personal and advisory:
        raise ValueError('incompatible_policy_modes')
    path = Path(checkpoint)
    target = digest(client.base)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.is_symlink():
            raise ValueError('checkpoint_symlink')
        policy = 'crm-personal-notes/2026-09-28-v2.1' if personal else 'note_interest' if advisory else 'call_facts'
        state = json.loads(path.read_text()) if path.exists() else {'manifest': digest(entries), 'target': target, 'policy': policy, 'items': {}}
        if (state['manifest'] != digest(entries) or state.get('target') != target
                or state.get('policy', 'call_facts') != policy):
            raise ValueError('manifest_or_target_changed')
        processed = 0
        advisories = {}  # Only fresh readbacks from this invocation, never cached hints.
        for entry in entries:
            key = entry['work_id']
            prior = state['items'].get(key)
            resumable = ('pending', 'inflight', 'uncertain', 'receipt_pending') if reconcile else ('pending',)
            if reconcile and prior and prior.get('status') == 'disabled' and not prior.get('receipt_id'):
                resumable += ('disabled',)
            if (prior is not None and prior.get('status') not in resumable) or processed >= limit:
                continue
            processed += 1
            item = {'status': 'inflight'}
            if prior and prior.get('receipt_id'):
                item['receipt_id'] = prior['receipt_id']
            state['items'][key] = item
            _save_checkpoint(path, state)  # Durable intent before first remote effect.
            try:
                before = client.call('GET', f'/api/v1/agent/work/{key}')
                source = before['context']['note_source']
                if (before['id'] != key or before['kind'] != 'call_followup' or before['lead_id'] != entry['lead_id']
                        or (before['context']['suppressed'] and not personal) or source['status'] != 'current'
                        or source['activity_type'] not in (('note','call') if personal else ('note' if advisory else 'call',))
                        or any(source[k] != entry[k] for k in ('activity_id', 'source_digest'))):
                    item['status'] = 'source_conflict'
                else:
                    route = 'jev-classify-personal' if personal else 'jev-classify-advisory' if advisory else 'jev-classify'
                    receipt = client.call('POST', f'/api/v1/agent/work/{key}/{route}')
                    if item.get('receipt_id') and receipt.get('receipt_id') != item['receipt_id']:
                        item['status'] = 'receipt_conflict'
                        _save_checkpoint(path, state)
                        continue
                    item.update({k: receipt[k] for k in ('receipt_id', 'usage', 'model', 'cost_status') if k in receipt})
                    if receipt.get('application', {}).get('status') == 'conflict':
                        item['status'] = 'receipt_conflict'
                    elif receipt.get('status') != 'succeeded':
                        item['status'] = 'receipt_pending' if receipt.get('status') == 'pending' else receipt.get('status', 'uncertain')
                    elif personal:
                        if receipt.get('version') != policy:
                            item['status'] = 'receipt_conflict'
                            _save_checkpoint(path, state)
                            continue
                        applied = client.call('POST', f'/api/v1/agent/work/{key}/personal-facts', {
                            'jev_receipt_id':receipt['receipt_id'], 'source_digest':entry['source_digest'],
                            'expected_lead_version':before['context']['lead_version']})
                        facts = client.call('GET', f"/api/v1/agent/leads/{entry['lead_id']}/notes/{entry['activity_id']}/personal-facts")
                        after = client.call('GET', f'/api/v1/agent/work/{key}')
                        if (not _work_preserved(before, after, personal=True) or facts.get('source_status') != 'current'
                                or facts.get('activity_id') != entry['activity_id']
                                or facts.get('source_digest') != entry['source_digest']
                                or facts.get('receipt_id') != receipt['receipt_id']
                                or facts.get('audit_id') != applied.get('audit_id')
                                or not isinstance(facts.get('classification'),dict)
                                or digest(facts['classification']) != digest(applied.get('classification'))
                                or facts.get('policy_version') != policy
                                or applied.get('source_work_unchanged') is not True):
                            item['status'] = 'readback_conflict'
                        else:
                            item.update(status='verified', audit_id=applied['audit_id'], facts_digest=digest(facts))
                    elif advisory:
                        current = _verify_advisory(client, entry, before, receipt, item)
                        if current is not None:
                            advisories[key] = current
                    else:
                        applied = client.call('POST', f'/api/v1/agent/work/{key}/note-facts', {
                            'jev_receipt_id': receipt['receipt_id'], 'source_digest': entry['source_digest'],
                            'expected_lead_version': before['context']['lead_version']})
                        facts = client.call('GET', f"/api/v1/agent/leads/{entry['lead_id']}/notes/{entry['activity_id']}/facts")
                        after = client.call('GET', f'/api/v1/agent/work/{key}')
                        preserved = _work_preserved(before, after)
                        interest = applied.get('expressed_commercial_interest')
                        interest_preserved = (interest is None or all(
                            (facts.get('expressed_commercial_interest') or {}).get(k) == interest.get(k)
                            for k in ('value', 'reason')))
                        adjudication = _reviewed_adjudication(entry, before, applied, facts)
                        if (facts['activity_id'] != entry['activity_id'] or facts['source_status'] != 'current'
                                or not preserved or not interest_preserved
                                or facts['source_digest'] != entry['source_digest'] or facts['conflict']
                                or (adjudication is None and any(k not in facts['derived_facts'] or type(facts['derived_facts'][k]) is not type(v)
                                    or facts['derived_facts'][k] != v for k, v in applied['facts'].items()))
                                or applied['source_work_unchanged'] is not True
                                or (applied.get('jev') or {}).get('receipt_id') != receipt['receipt_id']
                                or not any((p.get('jev') or {}).get('receipt_id') == receipt['receipt_id']
                                    and p.get('audit_id') == applied['audit_id'] for p in facts['provenance'])):
                            item['status'] = 'readback_conflict'
                        else:
                            has_interest = interest is not None and interest.get('value') in ('yes', 'no')
                            item.update(status='reviewed_adjudication' if adjudication else 'verified' if applied['facts'] or has_interest else 'abstained',
                                        audit_id=applied['audit_id'], facts_digest=digest(facts))
                            if adjudication:
                                item['adjudication'] = adjudication
            except Exception as error:
                # Unknown exception strings may contain credentials or private bodies.
                # Auth middleware's 429 is a confirmed rejection before endpoint
                # execution. A later pass can resume this exact source/work; its
                # canonical receipt also protects any earlier completed classify.
                item['status'] = 'pending' if getattr(error, 'http_status', None) == 429 else 'uncertain'
                if item['status'] == 'pending':
                    item['reason'] = 'api_rate_limited'
            _save_checkpoint(path, state)
            if item['status'] == 'pending':
                break  # Respect the shared bucket; no retries in this pass.
        counts = Counter(state['items'].get(i['work_id'], {}).get('status', 'pending') for i in entries)
        result = {'total': len(entries), 'counts': dict(counts), 'pending': counts.get('pending', 0),
                  'checkpoint': str(path), 'scope': 'manifest only; not full pipeline hygiene'}
        if advisories:
            result['advisories'] = advisories
        return result
