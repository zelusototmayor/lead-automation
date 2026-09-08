"""Read-only, pinned, tenant-scoped A1 proof resolver. Never fetches source URLs."""
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from src.crm.domain.phone_proof_contract import Registry, PhoneProof, EvidenceRef
from src.crm.persistence.models import Lead, Evidence, SourceIdentity


class PhoneEvidenceError(ValueError):
    def __init__(self, code='phone_evidence_invalid', status=422):
        self.code, self.status = code, status
        super().__init__(code)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PhoneEvidenceError()
            result[key] = value
        return result
    def constant(_):
        raise PhoneEvidenceError()
    try:
        return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)
    except (ValueError,UnicodeError):
        raise PhoneEvidenceError() from None


class PhoneProofBundle:
    """Deployment must mount the bundle read-only. No request can configure it."""
    def __init__(self, workspace_id):
        self.workspace_id = str(workspace_id)
        directory = os.environ.get('CRM_PHONE_PROOF_BUNDLE_DIR')
        pin = os.environ.get('CRM_PHONE_PROOF_REGISTRY_SHA256')
        self.registry = None
        self.digest = None
        if not directory and not pin:
            return
        if not directory or not pin or not Path(directory).is_absolute():
            raise PhoneEvidenceError('phone_evidence_unavailable',503)
        self.root = Path(directory)
        if self.root.resolve() != self.root:
            raise PhoneEvidenceError()
        try:
            raw = self.read('registry.json',pin,maximum=4*1024*1024)
            self.registry = Registry.model_validate(_json(raw))
            self.digest = pin
        except PhoneEvidenceError:
            raise
        except ValueError:
            raise PhoneEvidenceError() from None
        if self.registry.workspace_id != self.workspace_id:
            raise PhoneEvidenceError()

    def read(self, relative, digest, maximum=32*1024*1024):
        path = self.root / relative
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise PhoneEvidenceError()
        try:
            with path.open('rb') as source:
                raw = source.read(maximum+1)
        except OSError:
            raise PhoneEvidenceError('phone_evidence_unavailable',503) from None
        if len(raw)>maximum or hashlib.sha256(raw).hexdigest() != digest:
            raise PhoneEvidenceError()
        return raw

    def proof(self, proof_id, expected_hash=None):
        if self.registry is None:
            raise PhoneEvidenceError()
        entry = next((p for p in self.registry.proofs if p.proof_id == proof_id),None)
        if not entry or entry.status != 'active' or (expected_hash is not None and expected_hash != entry.sha256):
            raise PhoneEvidenceError()
        self.read(f'reviews/{entry.review_sha256}.md',entry.review_sha256)
        try:
            proof = PhoneProof.model_validate(_json(self.read(f'proofs/{entry.proof_id}.json',entry.sha256,maximum=256*1024)))
        except ValueError as exc:
            if isinstance(exc,PhoneEvidenceError):
                raise
            raise PhoneEvidenceError() from None
        if (proof.proof_id,proof.kind,proof.producer,proof.workspace_id) != (entry.proof_id,entry.kind,entry.producer,self.workspace_id):
            raise PhoneEvidenceError()
        source_digests = {s.snapshot_sha256 for s in proof.sources}
        if proof.claim.assertion_snapshot_sha256 not in source_digests:
            raise PhoneEvidenceError()
        for digest in source_digests:
            if digest not in {s.sha256 for s in self.registry.snapshots}:
                raise PhoneEvidenceError()
            self.read(f'snapshots/{digest}.bin',digest)
        for binding in proof.evidence_bindings:
            if binding.content_hash != binding.snapshot_sha256 or binding.snapshot_sha256 not in source_digests:
                raise PhoneEvidenceError()
        return proof

    def resolve_refs(self, session, lead_id, refs):
        parsed = [EvidenceRef.model_validate(r) for r in refs]
        if len(parsed)>8 or len({(r.source_system,r.source_id) for r in parsed}) != len(parsed):
            raise PhoneEvidenceError()
        proofs = [self.proof(r.source_id,r.sha256) for r in parsed if r.source_system == 'crm_phone_proof_v1']
        for proof in proofs:
            if proof.subject_lead_ids != [str(lead_id)] or proof.identity_proof_id is not None:
                raise PhoneEvidenceError()
            lead = session.scalar(select(Lead).where(Lead.workspace_id == UUID(self.workspace_id),Lead.id == lead_id))
            if lead is None:
                raise PhoneEvidenceError()
            for binding in proof.evidence_bindings:
                self.check_evidence_binding(session,binding)
        for ref in parsed:
            if ref.source_system == 'crm_evidence':
                binding = next((b for p in proofs for b in p.evidence_bindings if b.evidence_id == ref.source_id and b.content_hash == ref.sha256),None)
                if binding is None:
                    raise PhoneEvidenceError()
        return proofs

    def check_evidence_binding(self,session,binding):
        row = session.scalar(select(Evidence).where(Evidence.workspace_id == UUID(self.workspace_id),Evidence.id == UUID(binding.evidence_id)))
        source = session.scalar(select(SourceIdentity).where(SourceIdentity.workspace_id == UUID(self.workspace_id),SourceIdentity.id == UUID(binding.source_identity_id)))
        if row is None or source is None:
            raise PhoneEvidenceError()
        for name in ('account_id','source_identity_id','evidence_type','content_hash'):
            if str(getattr(row,name)) != str(getattr(binding,name)):
                raise PhoneEvidenceError()
        for name in ('source_system','source_scope','entity_kind','external_id'):
            if getattr(source,name) != getattr(binding,name):
                raise PhoneEvidenceError()


def validate_phone_history(session,workspace_id,lead_id,history):
    if history.state == 'unknown' and not history.evidence_refs:
        return
    bundle = PhoneProofBundle(workspace_id)
    proofs = bundle.resolve_refs(session,lead_id,history.model_dump(mode='json')['evidence_refs'])
    if history.state == 'prior_answer' and any(p.kind == 'prior_phone_answer' for p in proofs):
        return
    if history.state == 'unknown':
        return
    raise PhoneEvidenceError()
