"""Trusted offline migration only; NOT exposed as an answer-upload API.

The guarded runner verifies frozen file hashes and real Ledger reservations.
This bridge validates raw grouped answers and compares every observed source
field to locked live data. Timeline digest is retained, never relabelled as the
canonical digest. No HTTP, source edits, task creation or budget reservation.
"""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid5
from sqlalchemy import select
from src.crm.persistence.models import Activity, AgentWork, Lead
from src.crm.services.note_plan_service import _locked_company_lead, _locked_rows
from src.crm.services.note_source_service import source_digest
from src.crm.services.jose_note_sources import prepare_source
from src.crm.services.jev_note_policy import digest
from src.crm.services.jev_note_runtime import target_identity
from src.crm.services.jev_note_worker import load_snapshot, _receipt, KIND
from src.crm.services import jose_note_policy as policy


def import_source(session, principal, *, sources, receipt, event_id,
                  expected_lead_version, expected_source_digest):
    from scripts.jose_offline_batch import group_request
    requests, body = group_request(sources)
    ids = [r['event_id'] for r in requests]
    identity = 'jose-notes-v1:' + digest(body | {'sources':[(r['event_id'], r['source_digest']) for r in requests]})
    if (len(ids) != len(set(ids)) or event_id not in ids or receipt.get('id') != identity
            or receipt.get('simulated') is not False or receipt.get('model') != body['model']
            or receipt.get('request_digest') != digest(body) or receipt.get('event_ids') != ids
            or receipt.get('source_digests') != [r['source_digest'] for r in requests]
            or receipt.get('reserved_microeur') != 5376):
        raise ValueError('offline_receipt_binding_invalid')
    raw = receipt['provider_response']
    usage = raw.get('usage')
    if (raw.get('model') != body['model'] or set(raw.get('answers', {})) != set(body['questions'])
            or not isinstance(usage, dict) or set(usage) != {'input_tokens','output_tokens'}
            or any(type(v) is not int or v < 0 for v in usage.values())
            or usage['input_tokens'] > 64000 or usage != receipt.get('usage')):
        raise ValueError('offline_response_invalid')
    index = ids.index(event_id)
    original = sources[index]
    observed = original['source']
    if original['workspace_id'] != str(principal.workspace_id):
        raise ValueError('offline_workspace_changed')
    lead_id = UUID(original['lead_id'])
    if session.scalar(select(Lead.id).where(Lead.workspace_id == principal.workspace_id, Lead.id == lead_id)) is None:
        raise ValueError('offline_lead_changed')
    lead, _ = _locked_company_lead(session, principal.workspace_id, lead_id)
    rows = _locked_rows(session, select(Activity).where(Activity.workspace_id == principal.workspace_id,
        Activity.lead_id == lead_id))
    source = next((row for row in rows if str(row.id) == event_id), None)
    if (source is None or lead.version != expected_lead_version
            or source_digest(source) != expected_source_digest
            or any(row.supersedes_activity_id == source.id for row in rows)
            or observed['summary'] != source.summary or observed['activity_type'] != source.activity_type
            or observed.get('outcome_code') != source.outcome_code or observed.get('call_details') != source.call_details
            or datetime.fromisoformat(observed['occurred_at']) != source.occurred_at
            or observed['provenance'].get('actor_type') != source.actor_type
            or observed['provenance'].get('title') != source.title
            or (observed['provenance'].get('source_system') is not None
                and observed['provenance']['source_system'] != source.source_system)):
        raise ValueError('offline_source_changed')
    # Unknown source_system in the old timeline is explicit missing evidence,
    # not a claim of equality. actor_type/title/text must match exactly above.
    answers = {key:raw['answers'][f'n{index}_{key}'] for key in policy.QUESTIONS}
    decisions = policy.map_response(requests[index], {'model':raw['model'],'answers':answers})
    target = target_identity(session)
    key = 'jev-personal-note:' + digest({'target':target, 'event_id':event_id, 'version':policy.VERSION})
    receipt_id = uuid5(principal.workspace_id, key)
    prior = session.get(AgentWork, receipt_id)
    binding = {'receipt_sha256':digest(receipt),'timeline_source_digest':observed['source_digest'],
        'original_decision_id':requests[index]['decision_id'],'provider_request_digest':receipt['request_digest'],
        'ledger_reservation_id':identity,'group_index':index,
        'source_system_not_in_original_timeline':observed['provenance'].get('source_system') is None}
    if prior is not None:
        if (prior.workspace_id != principal.workspace_id or prior.kind != KIND
                or prior.result_hash != digest(prior.result)
                or prior.result.get('offline_import') != binding
                or prior.payload.get('source_digest') != expected_source_digest):
            raise ValueError('offline_import_collision')
        return {'receipt_id':str(prior.id),'work_id':prior.result['work_id'],'replayed':True}
    prepared = prepare_source(session, principal, lead_id, source.id,
        SimpleNamespace(expected_lead_version=expected_lead_version, source_digest=expected_source_digest))
    work_id = UUID(prepared['work_id'])
    snapshot = load_snapshot(session, principal.workspace_id, lead_id,
        {'activity_id':event_id, 'source_digest':expected_source_digest}, flags=policy.FLAGS)
    request = policy.build_request(snapshot)
    # Canonical request is a fresh binding, NOT the request sent to the provider.
    # Provenance retains both identities and raw original answers transparently.
    for value in decisions.values():
        value['source_digest'] = expected_source_digest
    result = _receipt(request, simulated=False)
    result.update(receipt_id=str(receipt_id), work_id=str(work_id), status='succeeded',
        decisions=decisions, target=target, usage=deepcopy(usage), raw_answers=deepcopy(answers),
        reserved_microeur=0, cost_status='offline_group_reserved_not_billed', offline_import=binding)
    row = AgentWork(id=receipt_id, workspace_id=principal.workspace_id, lead_id=lead_id,
        source_key=key, kind=KIND, status='completed', attempts=0,
        payload={k:request[k] for k in ('event_id','source_digest','snapshot_digest','flags')},
        result=result, result_hash=digest(result))
    session.add(row); session.flush()
    return {'receipt_id':str(receipt_id),'work_id':str(work_id),'replayed':False}
