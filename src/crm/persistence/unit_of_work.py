"""Caller-owned SQLAlchemy unit of work for CRM aggregate transactions."""

from __future__ import annotations

import hashlib
from types import TracebackType
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from src.crm.persistence.models import Account, Activity, Contact, Lead
from src.crm.persistence.repositories import (
    AccountRepository,
    ActivityRepository,
    ContactRepository,
    IngestEventRepository,
    LeadRepository,
    SourceIdentityRepository,
)


class SqlAlchemyUnitOfWork:
    """Open one session, commit explicitly once, and rollback every other exit."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory
        self.session: Session | None = None
        self._committed = False

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self.session = self.session_factory()
        self._committed = False
        self.accounts = AccountRepository(self.session)
        self.contacts = ContactRepository(self.session)
        self.leads = LeadRepository(self.session)
        self.activities = ActivityRepository(self.session)
        self.source_identities = SourceIdentityRepository(self.session)
        self.ingest_events = IngestEventRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        del exc, tb
        assert self.session is not None
        try:
            if exc_type is not None or not self._committed:
                self.session.rollback()
        finally:
            self.session.close()
        return False

    def commit(self) -> None:
        assert self.session is not None
        if self._committed:
            raise RuntimeError("unit of work already committed")
        self.session.commit()
        self._committed = True

    def rollback(self) -> None:
        assert self.session is not None
        self.session.rollback()
        self._committed = False

    def lock_identities(self, workspace_id: UUID, fingerprints: Iterable[str]) -> None:
        assert self.session is not None
        for fingerprint in sorted(set(fingerprints)):
            digest = hashlib.sha256(f"{workspace_id}:{fingerprint}".encode()).digest()
            key = int.from_bytes(digest[:8], "big", signed=True)
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
            )

    def lock_activity_replay(
        self, workspace_id: UUID, ingest_event_id: UUID, activity_type: str
    ) -> bool:
        self.lock_identities(
            workspace_id, (f"activity:{ingest_event_id}:{activity_type}",)
        )
        return (
            self.ingest_events.get(workspace_id, ingest_event_id, for_update=True)
            is not None
        )

    def claim_stage_reduction(
        self, workspace_id: UUID, ingest_event_id: UUID, fingerprint: str
    ) -> bool:
        from src.crm.services.account_service import ReplayConflictError

        event = self.ingest_events.get(workspace_id, ingest_event_id, for_update=True)
        if event is None:
            return False
        if event.stage_reduction_fingerprint is None:
            event.stage_reduction_fingerprint = fingerprint
        elif event.stage_reduction_fingerprint != fingerprint:
            raise ReplayConflictError(
                "ingest event already records different semantics"
            ) from None
        assert self.session is not None
        self.session.flush()
        return True

    def replay(self, workspace_id: UUID, ingest_event_id: UUID):
        activity = self.activities.replay(workspace_id, ingest_event_id, "stage_change")
        if activity is None:
            return None
        lead = (
            self.leads.get(workspace_id, activity.lead_id, for_update=True)
            if activity.lead_id
            else None
        )
        return activity, lead

    def activity_replay(
        self, workspace_id: UUID, ingest_event_id: UUID, activity_type: str
    ) -> Activity | None:
        return self.activities.replay(workspace_id, ingest_event_id, activity_type)

    def account_candidates(self, workspace_id: UUID, hints: Any) -> list[Account]:
        from src.crm.services.account_service import (
            IdentityReviewRequired,
            normalize_company_name,
        )

        candidates: dict[UUID, Account] = {}
        if hints.account_id:
            account = self.accounts.get(workspace_id, hints.account_id)
            if account is None:
                raise IdentityReviewRequired("identity requires review") from None
            candidates[account.id] = account
        if hints.source_identity_id:
            identity = self.source_identities.get(
                workspace_id, hints.source_identity_id
            )
            if identity is None or identity.entity_kind != "account":
                raise IdentityReviewRequired("identity requires review") from None
            if identity.canonical_entity_id is not None:
                if identity.canonical_entity_type != "account":
                    raise IdentityReviewRequired("identity requires review") from None
                account = self.accounts.get(workspace_id, identity.canonical_entity_id)
                if account is None:
                    raise IdentityReviewRequired("identity requires review") from None
                candidates[account.id] = account
        if hints.contact_email:
            for account in self.accounts.by_contact_email(
                workspace_id, hints.contact_email
            ):
                candidates[account.id] = account
        company = hints.company_name or hints.display_name or hints.legal_name
        if hints.domain and company:
            for account in self.accounts.by_domain_name(
                workspace_id, hints.domain, normalize_company_name(company)
            ):
                candidates[account.id] = account
        return list(candidates.values())

    def new_account(self, workspace_id: UUID, hints: Any) -> Account:
        from src.crm.services.account_service import normalize_company_name

        company = hints.company_name or hints.display_name or hints.legal_name
        return self.accounts.add(
            Account(
                workspace_id=workspace_id,
                legal_name=hints.legal_name,
                display_name=company,
                normalized_name=normalize_company_name(company),
                primary_domain=hints.domain,
                sector=hints.sector,
                commercial_vertical=hints.vertical,
                source_origin=hints.source_origin,
                source_identity_id=hints.source_identity_id,
            )
        )

    def new_contact(self, workspace_id: UUID, account_id: UUID, hints: Any) -> Contact:
        from src.crm.services.account_service import IdentityReviewRequired

        existing = self.contacts.by_email(
            workspace_id, hints.contact_email, for_update=True
        )
        if existing is not None:
            if existing.account_id != account_id:
                raise IdentityReviewRequired("identity requires review") from None
            return existing
        return self.contacts.add(
            Contact(
                workspace_id=workspace_id,
                account_id=account_id,
                full_name=hints.contact_name,
                primary_email=hints.contact_email,
            )
        )

    def new_lead(self, workspace_id: UUID, hints: Any) -> Lead:
        return self.leads.add(
            Lead(
                workspace_id=workspace_id,
                sector=hints.sector,
                commercial_vertical=hints.vertical,
                source_origin=hints.source_origin,
                source_identity_id=hints.source_identity_id,
            )
        )

    def new_activity(self, **values: Any) -> Activity:
        # Composite activity FKs observe the final aggregate links, so persist
        # pending lead/contact/account mutations before inserting the activity.
        assert self.session is not None
        self.session.flush()
        return self.activities.add(Activity(**values))

    def link_source_identity(
        self, workspace_id: UUID, source_identity_id: UUID | None, account_id: UUID
    ) -> None:
        from src.crm.services.account_service import IdentityReviewRequired

        if source_identity_id is None:
            return
        identity = self.source_identities.get(
            workspace_id, source_identity_id, for_update=True
        )
        if identity is None or identity.entity_kind != "account":
            raise IdentityReviewRequired("identity requires review") from None
        if identity.canonical_entity_id not in (None, account_id):
            raise IdentityReviewRequired("identity requires review") from None
        identity.canonical_entity_type = "account"
        identity.canonical_entity_id = account_id
        assert self.session is not None
        self.session.flush()

    def validate_activity_references(self, values: dict[str, object]) -> None:
        workspace_id = values["workspace_id"]
        account_id = values["account_id"]
        assert isinstance(workspace_id, UUID)
        assert account_id is None or isinstance(account_id, UUID)
        lead_id = values["lead_id"]
        lead = None
        if lead_id is not None:
            assert isinstance(lead_id, UUID)
            lead = self.leads.get(workspace_id, lead_id, for_update=True)
            if lead is None:
                raise ValueError("activity requires review") from None
        account = (
            self.accounts.get(workspace_id, account_id, for_update=True)
            if account_id is not None
            else None
        )
        if account_id is not None and account is None:
            raise ValueError("activity requires review") from None
        if lead is not None and lead.account_id != account_id:
            raise ValueError("activity requires review") from None
        if lead is None and account_id is None:
            raise ValueError("activity requires review") from None
        contact_id = values["contact_id"]
        if contact_id is not None:
            assert isinstance(contact_id, UUID)
            if account_id is None:
                raise ValueError("activity requires review") from None
            contact = self.contacts.get(workspace_id, contact_id, for_update=True)
            if contact is None or contact.account_id != account_id:
                raise ValueError("activity requires review") from None
        source_identity_id = values["source_identity_id"]
        if source_identity_id is not None:
            assert isinstance(source_identity_id, UUID)
            if (
                self.source_identities.get(
                    workspace_id, source_identity_id, for_update=True
                )
                is None
            ):
                raise ValueError("activity requires review") from None
        supersedes_id = values["supersedes_activity_id"]
        if supersedes_id is not None:
            assert isinstance(supersedes_id, UUID)
            supersedes = self.activities.get(workspace_id, supersedes_id)
            if supersedes is None or supersedes.account_id != account_id:
                raise ValueError("activity requires review") from None
