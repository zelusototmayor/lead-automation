"""A1 immutable proof syntax. Review/configuration pin, not prose, is authority."""
from typing import Annotated, Literal
from pydantic import (AfterValidator, AwareDatetime, BaseModel, BeforeValidator,
                      ConfigDict, Field, StrictInt, StrictStr, model_validator)

Hex64 = Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')]
CanonicalUUID = Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
VersionOne = Annotated[StrictInt, Field(ge=1, le=1)]
ProofKind = Literal['phone_baseline','prior_phone_answer','callback_agreement','lead_identity']
Producer = Literal['sales_source_review','crm_source_snapshot']


def _nonblank(value):
    if value != value.strip() or not value:
        raise ValueError('nonblank text required')
    return value


def _utc_wire(value):
    if type(value) is not str or not value.endswith('Z'):
        raise ValueError('UTC Z timestamp required')
    return value


UTCStamp = Annotated[AwareDatetime, BeforeValidator(_utc_wire)]
Text512 = Annotated[StrictStr, Field(min_length=1,max_length=512), AfterValidator(_nonblank)]
Text2000 = Annotated[StrictStr, Field(min_length=1,max_length=2000), AfterValidator(_nonblank)]


class StrictObject(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class EvidenceRef(StrictObject):
    source_system: Literal['crm_phone_proof_v1','crm_evidence']
    source_id: CanonicalUUID
    sha256: Hex64


class RegistryEntry(StrictObject):
    proof_id: CanonicalUUID
    sha256: Hex64
    kind: ProofKind
    producer: Producer
    review_sha256: Hex64
    status: Literal['active','revoked']


class SnapshotEntry(StrictObject):
    sha256: Hex64
    media_type: Literal['application/json','text/plain','application/pdf','application/octet-stream']
    producer: Producer
    captured_at: UTCStamp


class Registry(StrictObject):
    schema_version: VersionOne
    registry_id: CanonicalUUID
    workspace_id: CanonicalUUID
    proofs: list[RegistryEntry]
    snapshots: list[SnapshotEntry]

    @model_validator(mode='after')
    def unique_entries(self):
        if len({p.proof_id for p in self.proofs}) != len(self.proofs) or len({p.sha256 for p in self.proofs}) != len(self.proofs):
            raise ValueError('Duplicate proof entry')
        if len({p.sha256 for p in self.snapshots}) != len(self.snapshots):
            raise ValueError('Duplicate snapshot')
        return self


class ProofSource(StrictObject):
    snapshot_sha256: Hex64
    locator: Text512
    assertion_scope: Text2000


class EvidenceBinding(StrictObject):
    evidence_id: CanonicalUUID
    account_id: CanonicalUUID
    source_identity_id: CanonicalUUID
    evidence_type: Annotated[StrictStr, Field(min_length=1,max_length=32)]
    content_hash: Hex64
    source_system: Annotated[StrictStr, Field(min_length=1,max_length=32)]
    source_scope: Annotated[StrictStr, Field(min_length=1,max_length=255)]
    entity_kind: Annotated[StrictStr, Field(min_length=1,max_length=32)]
    external_id: Text512
    snapshot_sha256: Hex64


class PriorAnswerClaim(StrictObject):
    answered: Literal[True]
    counterparty: Literal['human_counterparty']
    occurred_at: UTCStamp | None
    date_precision: Literal['exact','unknown']
    assertion_snapshot_sha256: Hex64

    @model_validator(mode='before')
    @classmethod
    def strict_answer(cls, value):
        if isinstance(value,dict) and type(value.get('answered')) is not bool:
            raise ValueError('Exact boolean required')
        return value

    @model_validator(mode='after')
    def precision(self):
        if (self.date_precision == 'unknown') != (self.occurred_at is None):
            raise ValueError('Date precision mismatch')
        return self


class PhoneProof(StrictObject):
    schema_version: VersionOne
    proof_id: CanonicalUUID
    workspace_id: CanonicalUUID
    kind: ProofKind
    produced_at: UTCStamp
    producer: Producer
    subject_lead_ids: Annotated[list[CanonicalUUID], Field(min_length=1,max_length=1000)]
    identity_proof_id: CanonicalUUID | None
    sources: Annotated[list[ProofSource], Field(min_length=1,max_length=256)]
    evidence_bindings: Annotated[list[EvidenceBinding], Field(max_length=256)]
    claim: PriorAnswerClaim

    @model_validator(mode='after')
    def exact_subjects(self):
        if self.subject_lead_ids != sorted(set(self.subject_lead_ids)):
            raise ValueError('Subjects must be sorted and unique')
        if self.kind != 'prior_phone_answer':
            raise ValueError('Unsupported claim')
        return self
