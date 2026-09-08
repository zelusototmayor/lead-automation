"""Isolated, explicitly synthetic reviewed-source fixtures; never production proofs."""
import hashlib
import json
import pytest
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.crm.persistence.models import Lead, Activity, AuditEvent, OutboxEvent
from tests.integration.api.test_lead_operations_api import lead_operations_api, _headers


def write_json(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def proof_bundle(tmp_path, monkeypatch, workspace, lead):
    proof_id = str(uuid4())
    snapshot = b'ISOLATED TEST ONLY: source owner reports prior human answer, date unknown.\n'
    digest = hashlib.sha256(snapshot).hexdigest()
    (tmp_path/'snapshots').mkdir(exist_ok=True)
    (tmp_path/'snapshots'/f'{digest}.bin').write_bytes(snapshot)
    proof = {'schema_version':1,'proof_id':proof_id,'workspace_id':str(workspace),
             'kind':'prior_phone_answer','produced_at':'2026-09-01T12:00:00Z','producer':'sales_source_review',
             'subject_lead_ids':[str(lead)],'identity_proof_id':None,
             'sources':[{'snapshot_sha256':digest,'locator':'line:1','assertion_scope':'Test-only exact lead prior answer'}],
             'evidence_bindings':[],
             'claim':{'answered':True,'counterparty':'human_counterparty','occurred_at':None,
                      'date_precision':'unknown','assertion_snapshot_sha256':digest}}
    proof_hash = write_json(tmp_path/'proofs'/f'{proof_id}.json', proof)
    review = f'ISOLATED FIXTURE ONLY. proof={proof_id} sha256={proof_hash}; factual acceptance=test Sales; reviewer=test independent; not production.\n'.encode()
    review_hash = hashlib.sha256(review).hexdigest()
    (tmp_path/'reviews').mkdir(exist_ok=True)
    (tmp_path/'reviews'/f'{review_hash}.md').write_bytes(review)
    registry = {'schema_version':1,'registry_id':str(uuid4()),'workspace_id':str(workspace),
                'proofs':[{'proof_id':proof_id,'sha256':proof_hash,'kind':'prior_phone_answer',
                           'producer':'sales_source_review','review_sha256':review_hash,'status':'active'}],
                'snapshots':[{'sha256':digest,'media_type':'text/plain','producer':'sales_source_review',
                              'captured_at':'2026-09-01T11:00:00Z'}]}
    registry_hash = write_json(tmp_path/'registry.json', registry)
    monkeypatch.setenv('CRM_PHONE_PROOF_BUNDLE_DIR',str(tmp_path))
    monkeypatch.setenv('CRM_PHONE_PROOF_REGISTRY_SHA256',registry_hash)
    return proof, registry, {'source_system':'crm_phone_proof_v1','source_id':proof_id,'sha256':proof_hash}


def test_retained_reviewed_prior_answer_unknown_date_records_no_call(lead_operations_api, tmp_path, monkeypatch):
    client, engine, workspace, lead, _ = lead_operations_api
    with Session(engine) as session, session.begin():
        session.add(Lead(id=(lead := uuid4()), workspace_id=workspace, company_name='Accountless fixture'))
    proof, registry, ref = proof_bundle(tmp_path,monkeypatch,workspace,lead)
    history = {'schema_version':1,'state':'prior_answer','coverage_start_at':None,'covered_through_at':None,
               'evidence_refs':[ref],'reason':'Retained reviewed direct testimony; date unknown'}
    command = uuid4()
    body = {'command_id':str(command),'expected_version':1,'phone_history':history}
    url = f'/api/v1/commands/leads/{lead}/record-phone-history'
    result = client.post(url,json=body,headers=_headers(command))
    assert result.status_code == 200, result.text
    assert result.json()['phone_history'] == history
    with Session(engine) as session:
        assert session.get(Lead,lead).phone_history == history
        assert not session.scalars(select(Activity).where(Activity.workspace_id == workspace,Activity.activity_type == 'call')).all()
    # Revocation changes new authority, never an already committed receipt.
    registry['proofs'][0]['status'] = 'revoked'
    monkeypatch.setenv('CRM_PHONE_PROOF_REGISTRY_SHA256',write_json(tmp_path/'registry.json',registry))
    replay = client.post(url,json=body,headers=_headers(command))
    assert replay.json() == result.json() | {'replayed':True}
    other = uuid4()
    rejected = client.post(url,json=body|{'command_id':str(other),'expected_version':2},headers=_headers(other))
    assert rejected.status_code == 422
    with Session(engine) as session:
        assert session.get(Lead,lead).version == 2
        assert not session.scalars(select(OutboxEvent).where(OutboxEvent.command_id == other)).all()
        assert not session.scalars(select(AuditEvent).where(AuditEvent.command_id == other)).all()


@pytest.mark.parametrize('mutation,expected', [
    ('missing_snapshot',503),('missing_registry',503),('bad_pin',422),('tampered_review',422),
    ('tampered_proof',422),('duplicate_key',422),('extra_claim_key',422),('numeric_bool',422),
    ('cross_tenant',422),('other_subject',422),('unlisted',422),('symlink',422),
    ('duplicate_ref',422),('evidence_only',422),('metadata_only',422),('wrong_namespace',422),
])
def test_proof_failure_is_atomic_and_bounded(lead_operations_api,tmp_path,monkeypatch,mutation,expected):
    client, engine, workspace, lead, _ = lead_operations_api
    proof, registry, ref = proof_bundle(tmp_path,monkeypatch,workspace,lead)
    refs = [ref]
    if mutation == 'missing_snapshot':
        (tmp_path/'snapshots'/f"{registry['snapshots'][0]['sha256']}.bin").unlink()
    elif mutation == 'missing_registry':
        (tmp_path/'registry.json').unlink()
    elif mutation == 'bad_pin':
        monkeypatch.setenv('CRM_PHONE_PROOF_REGISTRY_SHA256','0'*64)
    elif mutation == 'tampered_review':
        (tmp_path/'reviews'/f"{registry['proofs'][0]['review_sha256']}.md").write_text('self approved')
    elif mutation == 'tampered_proof':
        (tmp_path/'proofs'/f"{proof['proof_id']}.json").write_text('{}')
    elif mutation == 'symlink':
        source = tmp_path/'proofs'/f"{proof['proof_id']}.json"
        target = tmp_path/'outside.json'
        source.rename(target)
        source.symlink_to(target)
    elif mutation == 'unlisted':
        ref['source_id'] = str(uuid4())
    elif mutation == 'duplicate_ref':
        refs.append(ref)
    elif mutation == 'evidence_only':
        ref['source_system'] = 'crm_evidence'
    elif mutation == 'metadata_only':
        refs = []
    elif mutation == 'wrong_namespace':
        ref['source_system'] = 'gmail'
    else:
        if mutation == 'extra_claim_key': proof['claim']['verified'] = True
        if mutation == 'numeric_bool': proof['claim']['answered'] = 1
        if mutation == 'cross_tenant': proof['workspace_id'] = str(uuid4())
        if mutation == 'other_subject': proof['subject_lead_ids'] = [str(uuid4())]
        path = tmp_path/'proofs'/f"{proof['proof_id']}.json"
        digest = write_json(path,proof)
        if mutation == 'duplicate_key':
            path.write_text(path.read_text().replace('"schema_version": 1','"schema_version": 1, "schema_version": 1'))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ref['sha256'] = registry['proofs'][0]['sha256'] = digest
        monkeypatch.setenv('CRM_PHONE_PROOF_REGISTRY_SHA256',write_json(tmp_path/'registry.json',registry))
    command = uuid4()
    history = {'schema_version':1,'state':'prior_answer','coverage_start_at':None,'covered_through_at':None,
               'evidence_refs':refs,'reason':'Synthetic negative fixture'}
    response = client.post(f'/api/v1/commands/leads/{lead}/record-phone-history',
                           json={'command_id':str(command),'expected_version':1,'phone_history':history},headers=_headers(command))
    assert response.status_code == expected, response.text
    with Session(engine) as session:
        assert session.get(Lead,lead).version == 1
        assert session.get(Lead,lead).phone_history is None
        assert not session.scalars(select(OutboxEvent).where(OutboxEvent.command_id == command)).all()
        assert not session.scalars(select(AuditEvent).where(AuditEvent.command_id == command)).all()
