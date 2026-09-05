"""Apply source-verified observations from a trusted Google collector.

Only exact existing contact/lead identity is eligible for account mutation.
Ambiguous observations become review work; no fuzzy company creation or sends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from uuid import uuid5

from sqlalchemy import or_, select
from src.crm.ingestion.processor import _claim_identity
from src.crm.persistence.models import AgentWork, Activity, Contact, EmailMessage, Lead
from src.crm.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.crm.services.agent_work_service import enqueue_work
from src.crm.services.evidence_service import EvidenceService, RecordEvidenceCommand
from src.crm.services.proposal_discovery_service import (
    ProposalDiscoveryService,
    DiscoverProposalCommand,
)


class ObservationError(ValueError):
    pass


def _bounded(value, maximum, *, required=True):
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or len(value.encode()) > maximum
        or (required and not value.strip())
    ):
        raise ObservationError("Invalid provider observation")
    return value


def apply_observation(session, workspace_id, observation):
    if not isinstance(observation, dict) or observation.get("provider") not in {
        "gmail",
        "google_calendar",
    }:
        raise ObservationError("Invalid provider observation")
    provider = observation["provider"]
    scope = _bounded(observation.get("source_scope"), 255)
    external_id = _bounded(observation.get("id"), 255)
    try:
        occurred = datetime.fromisoformat(
            observation["occurred_at"].replace("Z", "+00:00")
        )
        if occurred.tzinfo is None or occurred > datetime.now(UTC):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ObservationError("Invalid observation time") from None
    source_key = f"{provider}:{scope}:{external_id}"
    if provider == "google_calendar":
        source_key += ":" + occurred.isoformat()
    source_key = hashlib.sha256(source_key.encode()).hexdigest()
    # Borrow existing transaction; this UoW is never committed or closed here.
    uow = SqlAlchemyUnitOfWork(lambda: session).__enter__()
    uow.lock_identities(workspace_id, ("provider-observation:" + source_key,))
    existing = session.scalar(
        select(AgentWork).where(
            AgentWork.workspace_id == workspace_id, AgentWork.source_key == source_key
        )
    )
    if existing:
        return {"id": str(existing.id), "duplicate": True, "kind": existing.kind}
    if provider == "google_calendar":
        work_id = enqueue_work(
            session,
            workspace_id=workspace_id,
            source_key=source_key,
            kind="calendar_review",
            payload=observation,
        )
        return {"id": str(work_id), "duplicate": False, "kind": "calendar_review"}
    direction = observation.get("direction")
    if direction not in {"inbound", "outbound"}:
        raise ObservationError("Invalid message direction")
    subject = _bounded(observation.get("subject"), 512)
    excerpt = _bounded(observation.get("excerpt", ""), 8000, required=False)
    thread_id = _bounded(observation.get("thread_id"), 255)
    emails = observation.get("contact_emails")
    if (
        not isinstance(emails, list)
        or not 1 <= len(emails) <= 10
        or any(not isinstance(e, str) or len(e) > 320 or "@" not in e for e in emails)
    ):
        emails = []
    contacts = (
        list(
            session.scalars(
                select(Contact).where(
                    Contact.workspace_id == workspace_id,
                    Contact.primary_email.in_(emails),
                )
            )
        )
        if emails
        else []
    )
    candidates = (
        list(
            session.scalars(
                select(Lead).where(
                    Lead.workspace_id == workspace_id,
                    or_(
                        Lead.contact_email.in_(emails),
                        Lead.contact_id.in_([c.id for c in contacts]),
                    ),
                )
            )
        )
        if emails
        else []
    )
    # Exact one lead avoids associating multi-recipient or reused contacts wrongly.
    lead = candidates[0] if len(candidates) == 1 else None
    kind = "reply_review" if direction == "inbound" else "sent_review"
    attachments = observation.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 5:
        raise ObservationError("Invalid attachment observation")
    if direction == "outbound" and attachments:
        kind = "proposal_review"
    payload = {
        "provider": provider,
        "source_scope": scope,
        "message_id": external_id,
        "thread_id": thread_id,
        "occurred_at": occurred.isoformat(),
        "subject": subject,
        "excerpt": (excerpt or "")[:2000],
        "direction": direction,
        "contact_emails": emails,
        "attachments": attachments,
        "evidence": [
            {
                "provider": "gmail",
                "message_id": external_id,
                "thread_id": thread_id,
                "mailbox": scope,
            }
        ],
    }
    if lead is None:
        kind = (
            "identity_review" if attachments or candidates else "unmatched_observation"
        )
    if observation.get("bulk") is True:
        kind = "bulk_observation"
    if lead is not None and observation.get("bulk") is not True:
        payload["expected_lead_version"] = lead.version
        payload["company"] = lead.company_name
        message_identity = _claim_identity(
            session,
            workspace_id=workspace_id,
            source_system="gmail",
            source_scope=scope,
            entity_kind="message",
            external_id=external_id,
        )
        session.add(
            Activity(
                id=uuid5(workspace_id, "gmail-observed:" + source_key),
                workspace_id=workspace_id,
                account_id=lead.account_id,
                lead_id=lead.id,
                contact_id=lead.contact_id,
                activity_type="email_received"
                if direction == "inbound"
                else "email_sent",
                occurred_at=occurred,
                title=subject,
                summary=(excerpt or "").encode("utf-8")[:4000].decode("utf-8", "ignore")
                or None,
                direction=direction,
                source_system="gmail",
                source_identity_id=message_identity,
                actor_type="service",
                actor_id=uuid5(workspace_id, "google-collector"),
            )
        )
        if lead.account_id is not None:
            mailbox_identity = _claim_identity(
                session,
                workspace_id=workspace_id,
                source_system="gmail",
                source_scope=scope,
                entity_kind="mailbox",
                external_id=scope,
            )
            evidence = EvidenceService(uow).record(
                RecordEvidenceCommand(
                    workspace_id=workspace_id,
                    account_id=lead.account_id,
                    source_identity_id=message_identity,
                    evidence_type="email_message",
                    content_hash=hashlib.sha256(
                        json.dumps(payload, sort_keys=True).encode()
                    ).hexdigest(),
                    captured_at=occurred,
                    metadata={
                        "message_id": external_id,
                        "thread_id": thread_id,
                        "direction": direction,
                    },
                )
            )
            from sqlalchemy.dialects.postgresql import insert

            session.execute(
                insert(EmailMessage)
                .values(
                    workspace_id=workspace_id,
                    account_id=lead.account_id,
                    contact_id=lead.contact_id,
                    mailbox_identity_id=mailbox_identity,
                    provider_message_id=external_id,
                    provider_thread_id=thread_id,
                    direction=direction,
                    sent_at=occurred,
                    has_attachments=bool(attachments),
                    evidence_id=evidence.id,
                    subject=subject,
                )
                .on_conflict_do_nothing(
                    constraint="uq_email_messages_workspace_mailbox_provider"
                )
            )
            if direction == "outbound" and attachments:
                thread_identity = _claim_identity(
                    session,
                    workspace_id=workspace_id,
                    source_system="gmail",
                    source_scope=scope,
                    entity_kind="thread",
                    external_id=thread_id,
                )
                proposal_ids = []
                for attachment in attachments:
                    _bounded(attachment.get("name"), 512)
                    if attachment.get("currency") not in {
                        "EUR",
                        "USD",
                        "GBP",
                        "CHF",
                        "CAD",
                        "AUD",
                    }:
                        continue  # Unknown currency is explicitly a review, never invented EUR.
                    outcome = ProposalDiscoveryService(uow).discover(
                        DiscoverProposalCommand(
                            workspace_id=workspace_id,
                            account_id=lead.account_id,
                            message_source_identity_id=message_identity,
                            thread_source_identity_id=thread_identity,
                            occurred_at=occurred,
                            direction="outbound",
                            subject=subject,
                            classification="sent_attachment",
                            attachment_name=attachment["name"],
                            attachment_content_hash=attachment.get("content_hash"),
                            currency=attachment["currency"],
                            value_ambiguous=attachment.get("value_ambiguous", True),
                            one_off_amount=Decimal(attachment["one_off_amount"])
                            if attachment.get("one_off_amount")
                            else None,
                            mrr_amount=Decimal(attachment["mrr_amount"])
                            if attachment.get("mrr_amount")
                            else None,
                            arr_amount=Decimal(attachment["arr_amount"])
                            if attachment.get("arr_amount")
                            else None,
                        )
                    )
                    if outcome.proposal:
                        outcome.proposal.lead_id = lead.id
                        proposal_ids.append(str(outcome.proposal.id))
                payload["proposal_ids"] = proposal_ids
    work_id = enqueue_work(
        session,
        workspace_id=workspace_id,
        source_key=source_key,
        kind=kind,
        lead_id=lead.id if lead else None,
        payload=payload,
    )
    if kind in {"unmatched_observation", "bulk_observation"}:
        row = session.get(AgentWork, work_id)
        row.status = "completed"
        row.payload = {
            "provider": provider,
            "source_scope": scope,
            "message_id": external_id,
        }
        row.result = {
            "summary": "Sem ação comercial: fora dos contactos do CRM ou mensagem automática",
            "evidence": [],
        }
    session.flush()
    return {"id": str(work_id), "duplicate": False, "kind": kind}
