"""Strict schemas for the canonical FU protocol. No caller approval flags."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


BoundedReason = Annotated[str, Field(min_length=1, max_length=1000)]


class ModelVerdict(StrictBody):
    decision: Literal["APPROVE", "REJECT", "JOSE_REQUIRED"]
    reasons: list[BoundedReason] = Field(min_length=1, max_length=16)
    conditions: list[BoundedReason] = Field(max_length=16)
    risk_flags: list[BoundedReason] = Field(max_length=16)

    @model_validator(mode="after")
    def unconditional_approval(self):
        if self.decision == "APPROVE" and (self.conditions or self.risk_flags):
            raise ValueError("conditional_approval_forbidden")
        return self


Identity = Annotated[UUID, Field(strict=False)]
Hash = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
Opaque = Annotated[str, Field(min_length=1, max_length=255)]
Version = Annotated[int, Field(ge=1)]


class ReconcileBody(StrictBody):
    lead_id: Identity
    mailbox: Annotated[str, Field(min_length=3, max_length=254)]
    anchor_message_id: Opaque
    fu_stage: Annotated[int, Field(ge=1, le=5)]
    expected_lead_version: Version
    coverage_id: Hash
    context_hash: Hash


class SentContactSource(StrictBody):
    kind: Literal['gmail_sent']
    message_id: Opaque
    thread_id: Opaque
    message_sha256: Hash
    to: list[Annotated[str, Field(min_length=3, max_length=254)]] = Field(min_length=1, max_length=100)
    cc: list[Annotated[str, Field(min_length=3, max_length=254)]] = Field(max_length=100)
    header_to: Annotated[str, Field(min_length=3, max_length=32000)]
    header_cc: Annotated[str, Field(max_length=32000)]


class SentContactBody(StrictBody):
    command_id: Identity
    lead_id: Identity
    account_id: Identity
    mailbox: Annotated[str, Field(min_length=3, max_length=254)]
    expected_lead_version: Version
    expected_account_version: Version
    coverage_id: Hash
    context_hash: Hash
    context_observed_at: Annotated[AwareDatetime, Field(strict=False)]
    source: SentContactSource
    email: Annotated[str, Field(min_length=3, max_length=254)]
    make_primary: Literal[False]
    full_name: None
    title: None
    phone: None


class SentContactReceipt(StrictBody):
    schema: Literal['sent-secondary-contact/v1']
    command_id: Identity
    lead_id: Identity
    account_id: Identity
    mailbox: str
    contact_id: Identity
    contact_version: Version
    created: bool
    email: str
    make_primary: Literal[False]
    source_identity_id: Identity
    source: SentContactSource
    context_hash: Hash
    coverage_id: Hash
    lead_version: Version
    account_version: Version
    operational_contact_id: Identity | None
    external_actions: Literal['none']
    send_authority: Literal[False]


class SentContactState(StrictBody):
    contact_id: Identity
    version: Version
    primary_email: str | None
    full_name: str | None
    title: str | None
    phone: str | None
    is_primary: bool
    status: str


class SentContactReadback(StrictBody):
    receipt: SentContactReceipt
    verified: bool
    current_contact: SentContactState | None
    current_operational_contact_id: Identity | None


class ProjectionReconcileBody(StrictBody):
    expected_lead_version: Version
    context_hash: Hash


class AnchorQuote(StrictBody):
    message_id: Opaque
    quote: BoundedReason


class AnchorProposal(StrictBody):
    classification: Literal["intro", "fu1", "fu2", "fu3", "fu4", "fu5", "non_sequence", "uncertain"]
    supporting_message_ids: list[Opaque] = Field(min_length=1, max_length=100)
    evidence_quotes: list[AnchorQuote] = Field(min_length=1, max_length=32)
    rationale: BoundedReason


class AnchorPrepareBody(StrictBody):
    lease_token: Identity
    expected_lead_version: Version
    context_hash: Hash
    proposal: AnchorProposal


class PayloadPrepareBody(StrictBody):
    lease_token: Identity
    expected_lead_version: Version
    expected_task_version: Version
    context_hash: Hash
    payload_raw_b64: Annotated[str, Field(min_length=1, max_length=349528)]


class ExecuteBody(StrictBody):
    lease_token: Identity
    expected_lead_version: Version
    expected_task_version: Version
    context_hash: Hash
    payload_sha256: Hash
    reviewer_receipt_id: Identity


class RecoverBody(StrictBody):
    obligation_key: Annotated[str, Field(pattern="^fu:v1:[0-9a-f]{64}$")]


class VerdictBody(StrictBody):
    request_hash: Hash
    nonce: Hash
    context_hash: Hash
    payload_sha256: Hash
    signature_sha256: Hash
    runner_outcome: Literal["verdict", "timeout", "invalid_output", "process_error"]
    verdict: ModelVerdict | None
    reviewer_session_id: Annotated[str, Field(min_length=1, max_length=128)] | None

    @model_validator(mode="after")
    def complete_outcome(self):
        if self.runner_outcome == "verdict":
            if self.verdict is None or self.reviewer_session_id is None:
                raise ValueError("verdict_session_required")
        elif self.verdict is not None:
            raise ValueError("error_cannot_approve")
        return self
