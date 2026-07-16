"""Deterministic discovery of evidence-backed proposal candidates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.crm.domain.money import normalize_currency
from src.crm.persistence.models import Proposal, ProposalVersion, ReviewCandidate
from src.crm.services.evidence_service import EvidenceService, RecordEvidenceCommand
from src.crm.services.proposal_service import (
    AppendProposalVersionCommand,
    ProposalReviewRequired,
    ProposalService,
    SelectProposalVersionCommand,
)


@dataclass(frozen=True, slots=True)
class DiscoverProposalCommand:
    workspace_id: UUID
    account_id: UUID
    message_source_identity_id: UUID
    thread_source_identity_id: UUID
    occurred_at: datetime
    direction: str
    subject: str
    classification: str
    attachment_name: str | None = None
    attachment_content_hash: str | None = None
    currency: str | None = None
    one_off_amount: Decimal | None = None
    mrr_amount: Decimal | None = None
    arr_amount: Decimal | None = None
    value_ambiguous: bool = False
    extraction_confidence: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    action: str
    proposal: Proposal | None = None
    version: ProposalVersion | None = None
    review_candidate: ReviewCandidate | None = None


class ProposalDiscoveryService:
    """Apply explicit connector facts; never infer commercial state from prose."""

    def __init__(self, uow: Any):
        self.uow = uow
        self.evidence = EvidenceService(uow)
        self.proposals = ProposalService(uow)

    def discover(self, command: DiscoverProposalCommand) -> DiscoveryOutcome:
        self._validate(command)
        self.uow.lock_identities(
            command.workspace_id,
            (f"proposal-thread:{command.thread_source_identity_id}",),
        )
        message_evidence = self.evidence.record(
            RecordEvidenceCommand(
                workspace_id=command.workspace_id,
                account_id=command.account_id,
                source_identity_id=command.message_source_identity_id,
                evidence_type="email_message",
                content_hash=self._message_hash(command),
                captured_at=command.occurred_at,
                metadata={
                    "direction": command.direction,
                    "classification": command.classification,
                },
            )
        )
        if command.classification == "followup":
            return DiscoveryOutcome(action="ignored_followup")
        if command.classification == "promised":
            review = self._review_candidate(
                command, message_evidence.id, "send_promised_proposal", None
            )
            return DiscoveryOutcome(action="review", review_candidate=review)

        assert command.attachment_content_hash is not None
        attachment = self.evidence.record(
            RecordEvidenceCommand(
                workspace_id=command.workspace_id,
                account_id=command.account_id,
                source_identity_id=command.message_source_identity_id,
                evidence_type="attachment",
                content_hash=command.attachment_content_hash,
                captured_at=command.occurred_at,
                metadata={"filename": command.attachment_name},
            )
        )
        proposal = self.uow.proposals.by_thread(
            command.workspace_id, command.thread_source_identity_id, for_update=True
        )
        if proposal is not None:
            prior = next(
                (
                    version
                    for version in self.uow.proposal_versions.for_proposal(proposal.id)
                    if version.source_document_evidence_id == attachment.id
                ),
                None,
            )
            if prior is not None:
                review = self._existing_review(command, proposal)
                return DiscoveryOutcome(
                    action="review" if review else "proposal_candidate",
                    proposal=proposal,
                    version=prior,
                    review_candidate=review,
                )
        else:
            proposal = self.uow.proposals.add(
                Proposal(
                    workspace_id=command.workspace_id,
                    account_id=command.account_id,
                    thread_source_identity_id=command.thread_source_identity_id,
                    title=command.subject.strip(),
                    status="sent",
                    sent_at=command.occurred_at,
                    sent_evidence_id=message_evidence.id,
                    sent_verification_state="verified",
                    currency=normalize_currency(command.currency),
                    value_state="missing",
                    version=1,
                )
            )

        version = self.proposals.append_version(
            AppendProposalVersionCommand(
                workspace_id=command.workspace_id,
                proposal_id=proposal.id,
                expected_version=proposal.version,
                status="sent",
                sent_at=command.occurred_at,
                one_off_amount=None
                if command.value_ambiguous
                else command.one_off_amount,
                mrr_amount=None if command.value_ambiguous else command.mrr_amount,
                arr_amount=None if command.value_ambiguous else command.arr_amount,
                source_document_evidence_id=attachment.id,
                extraction_confidence=command.extraction_confidence,
            )
        )
        self.proposals.select_version(
            SelectProposalVersionCommand(
                workspace_id=command.workspace_id,
                proposal_id=proposal.id,
                version_id=version.id,
                expected_version=proposal.version,
            )
        )
        if command.value_ambiguous:
            review = self._review_candidate(
                command, attachment.id, "review_proposal_value", proposal.id
            )
            return DiscoveryOutcome("review", proposal, version, review)
        return DiscoveryOutcome("proposal_candidate", proposal, version)

    def _review_candidate(self, command, evidence_id, action_type, proposal_id):
        key = f"{action_type}:{command.thread_source_identity_id}"
        existing = self.uow.review_candidates.open_by_key(command.workspace_id, key)
        if existing is not None:
            return existing
        return self.uow.review_candidates.add(
            ReviewCandidate(
                workspace_id=command.workspace_id,
                account_id=command.account_id,
                proposal_id=proposal_id,
                evidence_id=evidence_id,
                action_type=action_type,
                dedupe_key=key,
                state="open",
            )
        )

    def _existing_review(self, command, proposal):
        if not command.value_ambiguous:
            return None
        return self._review_candidate(
            command, proposal.sent_evidence_id, "review_proposal_value", proposal.id
        )

    @staticmethod
    def _message_hash(command: DiscoverProposalCommand) -> str:
        stable = f"{command.message_source_identity_id}:{command.classification}"
        return hashlib.sha256(stable.encode()).hexdigest()

    @staticmethod
    def _validate(command: DiscoverProposalCommand) -> None:
        if type(command) is not DiscoverProposalCommand:
            raise ProposalReviewRequired("proposal discovery requires review")
        if command.classification not in {"promised", "sent_attachment", "followup"}:
            raise ProposalReviewRequired("proposal discovery requires review")
        if command.direction != "outbound":
            raise ProposalReviewRequired("proposal discovery requires review")
        if (
            not isinstance(command.occurred_at, datetime)
            or command.occurred_at.tzinfo is None
        ):
            raise ProposalReviewRequired("proposal discovery requires review")
        if (
            not isinstance(command.subject, str)
            or not command.subject.strip()
            or len(command.subject.strip()) > 512
        ):
            raise ProposalReviewRequired("proposal discovery requires review")
        if type(command.value_ambiguous) is not bool:
            raise ProposalReviewRequired("proposal discovery requires review")
        if command.classification == "sent_attachment":
            if (
                not command.attachment_name
                or normalize_currency(command.currency) is None
                or not isinstance(command.attachment_content_hash, str)
                or len(command.attachment_content_hash) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in command.attachment_content_hash
                )
            ):
                raise ProposalReviewRequired("proposal discovery requires review")
        elif any(
            value is not None
            for value in (
                command.attachment_name,
                command.attachment_content_hash,
                command.currency,
                command.one_off_amount,
                command.mrr_amount,
                command.arr_amount,
            )
        ):
            raise ProposalReviewRequired("proposal discovery requires review")
