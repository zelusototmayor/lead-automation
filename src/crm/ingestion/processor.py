"""Idempotent reduction of connector events into canonical CRM aggregates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from src.crm.ingestion.contracts import EventEnvelope
from src.crm.persistence.models import IngestEvent, SourceIdentity
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.account_service import (
    AccountService,
    IdentityHints,
    IdentityReviewRequired,
    StageTransitionCommand,
)
from src.crm.services.evidence_service import EvidenceService, RecordEvidenceCommand
from src.crm.services.proposal_discovery_service import (
    DiscoverProposalCommand,
    ProposalDiscoveryService,
)


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    event_id: UUID
    status: str


def _event_lock_key(workspace_id: UUID, event_id: UUID) -> int:
    digest = hashlib.sha256(f"{workspace_id}:{event_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _identity_external_id(scope: str, external_id: str, kind: str) -> str:
    canonical = json.dumps(
        [scope, external_id, kind], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _claim_identity(
    session: Session,
    *,
    workspace_id: UUID,
    source_system: str,
    source_scope: str,
    entity_kind: str,
    external_id: str,
) -> UUID:
    identity_id = uuid4()
    inserted = session.execute(
        insert(SourceIdentity)
        .values(
            id=identity_id,
            workspace_id=workspace_id,
            source_system=source_system,
            source_scope=source_scope,
            entity_kind=entity_kind,
            external_id=external_id,
            metadata_json={},
        )
        .on_conflict_do_nothing(
            index_elements=[
                SourceIdentity.workspace_id,
                SourceIdentity.source_system,
                SourceIdentity.source_scope,
                SourceIdentity.entity_kind,
                SourceIdentity.external_id,
            ]
        )
        .returning(SourceIdentity.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    existing = session.scalar(
        select(SourceIdentity.id).where(
            SourceIdentity.workspace_id == workspace_id,
            SourceIdentity.source_system == source_system,
            SourceIdentity.source_scope == source_scope,
            SourceIdentity.entity_kind == entity_kind,
            SourceIdentity.external_id == external_id,
        )
    )
    if existing is None:
        raise IdentityReviewRequired("identity requires review")
    return existing


class _BorrowedUnitOfWork:
    """Expose an outer UoW without letting a nested service commit it."""

    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self._uow = uow

    def __enter__(self) -> "_BorrowedUnitOfWork":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def __getattr__(self, name: str):
        return getattr(self._uow, name)

    def commit(self) -> None:
        """The event processor owns the only physical commit."""


def _text_fact(facts: dict[str, Any], name: str) -> str | None:
    value = facts.get(name)
    if value is None:
        return None
    if type(value) is not str or not value.strip():
        raise IdentityReviewRequired("identity requires review")
    return value.strip()


def _decimal_fact(facts: dict[str, Any], name: str) -> Decimal | None:
    value = facts.get(name)
    if value is None:
        return None
    if type(value) not in {str, int}:
        raise IdentityReviewRequired("identity requires review")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise IdentityReviewRequired("identity requires review") from None


def _classification(envelope: EventEnvelope) -> str:
    value = envelope.facts.get("classification")
    if type(value) is not str:
        return "review"
    return value


def _account_transition(
    uow: SqlAlchemyUnitOfWork,
    workspace_id: UUID,
    event_id: UUID,
    envelope: EventEnvelope,
    account_identity_id: UUID,
) -> tuple[str, UUID | None, UUID | None]:
    classification = _classification(envelope)
    if classification == "excluded":
        return "ignored", None, None
    if classification == "review":
        raise IdentityReviewRequired("identity requires review")

    if envelope.event_type == "gmail.message.observed":
        if classification == "sent_attachment":
            target_stage = "proposal_sent"
        elif classification == "promised":
            target_stage = "proposal_requested"
        elif classification == "followup":
            raise IdentityReviewRequired("identity requires review")
        else:
            raise IdentityReviewRequired("identity requires review")
    elif envelope.event_type == "calendar.meeting.observed":
        if classification != "confirmed":
            raise IdentityReviewRequired("identity requires review")
        target_stage = (
            "meeting_held"
            if envelope.facts.get("status") == "held"
            else "meeting_booked"
        )
    elif envelope.event_type == "meeting.note.observed":
        if classification != "confirmed":
            raise IdentityReviewRequired("identity requires review")
        target_stage = "meeting_held"
    else:
        raise IdentityReviewRequired("identity requires review")

    result = AccountService(lambda: _BorrowedUnitOfWork(uow)).apply_stage_transition(
        StageTransitionCommand(
            workspace_id=workspace_id,
            target_stage=target_stage,
            identity=IdentityHints(
                source_identity_id=account_identity_id,
                contact_email=_text_fact(envelope.facts, "contact_email"),
                company_name=_text_fact(envelope.facts, "company_name"),
                domain=_text_fact(envelope.facts, "domain"),
                sector=_text_fact(envelope.facts, "sector"),
                vertical=_text_fact(envelope.facts, "commercial_vertical"),
                source_origin=envelope.source.system,
            ),
            occurred_at=envelope.occurred_at,
            ingest_event_id=event_id,
            commercial_classification="confirmed",
        )
    )
    return "applied", result.account_id, result.lead_id


def _materialize_proposal(
    uow: SqlAlchemyUnitOfWork,
    workspace_id: UUID,
    event_id: UUID,
    envelope: EventEnvelope,
    account_id: UUID,
    lead_id: UUID,
) -> None:
    assert uow.session is not None
    message_identity_id = _claim_identity(
        uow.session,
        workspace_id=workspace_id,
        source_system=envelope.source.system,
        source_scope=envelope.source.scope,
        entity_kind="message",
        external_id=envelope.subject.external_id,
    )
    thread_id = _text_fact(envelope.facts, "thread_id")
    if thread_id is None:
        raise IdentityReviewRequired("identity requires review")
    lead = uow.leads.get(workspace_id, lead_id, for_update=True)
    if lead is None or lead.account_id != account_id:
        raise IdentityReviewRequired("identity requires review")
    if uow.activity_replay(workspace_id, event_id, "email_sent") is None:
        uow.new_activity(
            workspace_id=workspace_id,
            account_id=account_id,
            lead_id=lead_id,
            contact_id=lead.contact_id,
            activity_type="email_sent",
            occurred_at=envelope.occurred_at,
            title="Commercial email observed",
            direction="outbound",
            source_system=envelope.source.system,
            source_identity_id=message_identity_id,
            ingest_event_id=event_id,
        )
    thread_identity_id = _claim_identity(
        uow.session,
        workspace_id=workspace_id,
        source_system=envelope.source.system,
        source_scope=envelope.source.scope,
        entity_kind="thread",
        external_id=thread_id,
    )
    ProposalDiscoveryService(uow).discover(
        DiscoverProposalCommand(
            workspace_id=workspace_id,
            account_id=account_id,
            message_source_identity_id=message_identity_id,
            thread_source_identity_id=thread_identity_id,
            occurred_at=envelope.occurred_at,
            direction=_text_fact(envelope.facts, "direction") or "",
            subject="Proposal candidate",
            classification=_classification(envelope),
            attachment_name=_text_fact(envelope.facts, "attachment_name"),
            attachment_content_hash=_text_fact(
                envelope.facts, "attachment_content_hash"
            ),
            currency=_text_fact(envelope.facts, "currency"),
            one_off_amount=_decimal_fact(envelope.facts, "one_off_amount"),
            mrr_amount=_decimal_fact(envelope.facts, "mrr_amount"),
            arr_amount=_decimal_fact(envelope.facts, "arr_amount"),
            value_ambiguous=envelope.facts.get("value_ambiguous") is True,
        )
    )


def _materialize_meeting(
    uow: SqlAlchemyUnitOfWork,
    workspace_id: UUID,
    event_id: UUID,
    envelope: EventEnvelope,
    account_id: UUID,
    lead_id: UUID,
) -> None:
    assert uow.session is not None
    meeting_identity_id = _claim_identity(
        uow.session,
        workspace_id=workspace_id,
        source_system=envelope.source.system,
        source_scope=envelope.source.scope,
        entity_kind="meeting",
        external_id=envelope.subject.external_id,
    )
    prior = uow.activity_replay(workspace_id, event_id, "meeting")
    lead = uow.leads.get(workspace_id, lead_id, for_update=True)
    if lead is None or lead.account_id != account_id:
        raise IdentityReviewRequired("identity requires review")
    if prior is None:
        uow.new_activity(
            workspace_id=workspace_id,
            account_id=account_id,
            lead_id=lead_id,
            contact_id=lead.contact_id,
            activity_type="meeting",
            occurred_at=envelope.occurred_at,
            title="Commercial meeting observed",
            source_system=envelope.source.system,
            source_identity_id=meeting_identity_id,
            ingest_event_id=event_id,
        )
    if (
        envelope.event_type == "meeting.note.observed"
        and envelope.facts.get("has_notes") is True
    ):
        content_hash = hashlib.sha256(
            f"{meeting_identity_id}:notes-present".encode()
        ).hexdigest()
        EvidenceService(uow).record(
            RecordEvidenceCommand(
                workspace_id=workspace_id,
                account_id=account_id,
                source_identity_id=meeting_identity_id,
                evidence_type="meeting_note",
                content_hash=content_hash,
                captured_at=envelope.occurred_at,
                metadata={"has_notes": True},
            )
        )


def process_ingest_event(
    session_factory: sessionmaker[Session],
    workspace_id: UUID,
    event_id: UUID,
    *,
    before_commit: Callable[[], None] | None = None,
) -> ProcessOutcome:
    """Reduce one connector event atomically; retries are safe after crashes."""

    if type(workspace_id) is not UUID or type(event_id) is not UUID:
        raise ValueError("invalid processing request")

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert uow.session is not None
        session = uow.session
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _event_lock_key(workspace_id, event_id)},
        )
        event = session.scalar(
            select(IngestEvent)
            .where(
                IngestEvent.workspace_id == workspace_id,
                IngestEvent.id == event_id,
            )
            .with_for_update()
        )
        if event is None:
            raise ValueError("invalid processing request")
        if event.processing_status in {"applied", "ignored", "review"}:
            return ProcessOutcome(event.id, event.processing_status)
        envelope = EventEnvelope.model_validate(event.payload)
        if envelope.payload_hash() != event.payload_hash:
            raise ValueError("invalid processing request")

        try:
            with session.begin_nested():
                event.attempt_count += 1
                if _classification(envelope) == "excluded":
                    status = "ignored"
                    account_id = None
                    lead_id = None
                else:
                    account_external_id = _identity_external_id(
                        envelope.source.scope,
                        envelope.subject.external_id,
                        "account",
                    )
                    account_identity_id = _claim_identity(
                        session,
                        workspace_id=workspace_id,
                        source_system=envelope.source.system,
                        source_scope=envelope.source.scope,
                        entity_kind="account",
                        external_id=account_external_id,
                    )
                    status, account_id, lead_id = _account_transition(
                        uow,
                        workspace_id,
                        event_id,
                        envelope,
                        account_identity_id,
                    )
                    if status == "applied":
                        assert account_id is not None and lead_id is not None
                        if envelope.event_type == "gmail.message.observed":
                            _materialize_proposal(
                                uow,
                                workspace_id,
                                event_id,
                                envelope,
                                account_id,
                                lead_id,
                            )
                        elif envelope.event_type in {
                            "calendar.meeting.observed",
                            "meeting.note.observed",
                        }:
                            _materialize_meeting(
                                uow,
                                workspace_id,
                                event_id,
                                envelope,
                                account_id,
                                lead_id,
                            )
        except IdentityReviewRequired:
            event = session.scalar(
                select(IngestEvent)
                .where(
                    IngestEvent.workspace_id == workspace_id,
                    IngestEvent.id == event_id,
                )
                .with_for_update()
            )
            assert event is not None
            event.attempt_count += 1
            event.processing_status = "review"
            event.last_error_redacted = "identity requires review"
            event.next_attempt_at = None
            outcome = ProcessOutcome(event.id, "review")
        else:
            event.processing_status = status
            event.last_error_redacted = None
            event.next_attempt_at = None
            if status == "applied":
                event.applied_at = datetime.now(UTC)
            outcome = ProcessOutcome(event.id, status)

        if before_commit is not None:
            before_commit()
        uow.commit()
        return outcome
