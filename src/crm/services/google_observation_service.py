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

from sqlalchemy import or_, select, func
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


def apply_observation(session, workspace_id, observation, *, rebind_unmatched_lead_id=None, actor_id=None):
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
    recovering_unmatched = bool(existing and rebind_unmatched_lead_id is not None
        and existing.kind == "unmatched_observation" and existing.status == "completed"
        and existing.lead_id is None and existing.task_id is None
        and existing.lease_token is None and existing.attempts == 0)
    if existing and not recovering_unmatched:
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
    if "DRAFT" in observation.get("label_ids", []):
        raise ObservationError("Draft is not a sent or received email")
    direction = observation.get("direction")
    if direction not in {"inbound", "outbound"}:
        raise ObservationError("Invalid message direction")
    subject = _bounded(observation.get("subject"), 512)
    excerpt = _bounded(observation.get("excerpt", ""), 8000, required=False)
    thread_id = _bounded(observation.get("thread_id"), 255)
    attachments = observation.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 5:
        raise ObservationError("Invalid attachment observation")
    emails = observation.get("contact_emails")
    from src.crm.services.account_service import normalize_email
    try:
        if (
            not isinstance(emails, list)
            or not 1 <= len(emails) <= 10
            or any(not isinstance(e, str) or len(e) > 320 or "@" not in e for e in emails)
        ):
            raise ValueError("Invalid contact email identity")
        identity_emails = [normalize_email(value) for value in emails]
    except ValueError:
        if recovering_unmatched:
            raise ObservationError("Existing observation cannot be rebound") from None
        # Block the WHOLE observation before any recipient/draft matching or
        # business writes. Existing JSONB review storage preserves every fact;
        # waiting + no lead/task prevents claims and grants no send authority.
        from copy import deepcopy
        original = deepcopy(observation)
        blocker = {
            "reason": "invalid_contact_email_identity",
            "actionable": False,
            "source_sha256": hashlib.sha256(json.dumps(
                original, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode()).hexdigest(),
        }
        work_id = enqueue_work(
            session, workspace_id=workspace_id, source_key=source_key,
            kind="identity_review", payload={
                "provider": provider, "source_scope": scope,
                "message_id": external_id, "original_observation": original,
                "identity_blocker_v1": blocker,
            },
        )
        row = session.get(AgentWork, work_id)
        row.status = "waiting"
        row.result = {
            "summary": "Invalid contact email identity; whole observation blocked for review",
            "evidence": [], "identity_blocker_v1": blocker,
        }
        row.result_hash = hashlib.sha256(json.dumps(
            {"status": "waiting", "result": row.result},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        session.flush()
        return {"id": str(work_id), "duplicate": False, "kind": "identity_review"}
    contacts = (
        list(
            session.scalars(
                select(Contact).where(
                    Contact.workspace_id == workspace_id,
                    func.lower(Contact.primary_email).in_(identity_emails),
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
                        func.lower(Lead.contact_email).in_(identity_emails),
                        Lead.contact_id.in_([c.id for c in contacts]),
                        # A company can have several known contacts. A manual
                        # send to a secondary contact must enter the same lead.
                        # Exact email/account binding only; ambiguity stays open.
                        Lead.account_id.in_([c.account_id for c in contacts]),
                    ),
                )
            )
        )
        if emails
        else []
    )
    # Exact one lead avoids associating multi-recipient or reused contacts wrongly.
    lead = candidates[0] if len(candidates) == 1 else None
    from src.crm.services.email_update_service import active, matching_draft, enqueue_email_update
    matched_task = (matching_draft(session, workspace_id, observation, occurred)
        if active(occurred) and observation.get('bulk') is not True else None)
    if matched_task is not None:
        lead = session.get(Lead, matched_task.lead_id)
    if lead is None and not candidates and direction == 'inbound' and emails and active(occurred):
        # A reply can use the explicit alternate recipient of a note-created draft.
        # Require the same mailbox/thread and that sender among previous recipients.
        prior_ids = {row.lead_id for row in session.scalars(select(AgentWork).where(
            AgentWork.workspace_id == workspace_id, AgentWork.lead_id.is_not(None),
            AgentWork.payload['provider'].astext == 'gmail',
            AgentWork.payload['source_scope'].astext == scope,
            AgentWork.payload['thread_id'].astext == thread_id,
            AgentWork.payload['direction'].astext == 'outbound'))
            if set(e.casefold() for e in emails) &
               set(e.casefold() for e in row.payload.get('contact_emails', []))
            and datetime.fromisoformat(row.payload['occurred_at']) <= occurred}
        if len(prior_ids) == 1:
            lead = session.scalar(select(Lead).where(Lead.workspace_id == workspace_id,
                Lead.id == next(iter(prior_ids))))
    kind = "reply_review" if direction == "inbound" else "sent_review"
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
    if recovering_unmatched:
        # Only the canonical caller has verified this exact SENT against fresh
        # full context. Never reassign an already-bound or ambiguous observation.
        if (provider != 'gmail' or direction != 'outbound' or kind != 'sent_review'
                or observation.get('bulk') is True or lead is None
                or lead.id != rebind_unmatched_lead_id
                or session.get(Activity, uuid5(workspace_id, "gmail-observed:" + source_key)) is not None):
            raise ObservationError('Existing observation cannot be rebound')
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
    if recovering_unmatched:
        from copy import deepcopy
        from src.crm.persistence.models import AuditEvent
        from src.crm.services.followup_review import initial_anchor
        command=uuid5(existing.id,'verified-contact-rebind/v1')
        before={'kind':existing.kind,'status':existing.status,'payload':deepcopy(existing.payload),'result':deepcopy(existing.result)}
        existing.kind='sent_review';existing.status='queued';existing.lead_id=lead.id
        existing.payload=dict(payload,followup_anchor_v1=initial_anchor(scope[8:],lead.id,thread_id,external_id))
        existing.result=None;existing.error=None;existing.available_at=datetime.now(UTC)
        existing.updated_at=datetime.now(UTC)
        session.add(AuditEvent(id=command,command_id=command,workspace_id=workspace_id,
            actor_id=actor_id,action='agent.unmatched_email_rebound',entity_type='agent_work',entity_id=existing.id,
            details={'before':before,'lead_id':str(lead.id),'provider_message_id':external_id,
                     'contact_emails':emails,'fresh_verified_sent_context':True,'new_send_authority':False}))
        work_id=existing.id
    else:
        work_id = enqueue_work(
            session,workspace_id=workspace_id,source_key=source_key,kind=kind,
            lead_id=lead.id if lead else None,payload=payload,
        )
    if lead is not None and observation.get('bulk') is not True:
        session.flush()
        activity = session.get(Activity, uuid5(workspace_id, "gmail-observed:" + source_key))
        enqueue_email_update(session, lead, activity, payload, work_id, matched_task)
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
