"""SQLAlchemy repositories for workspace-scoped CRM aggregates."""

from __future__ import annotations

from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.crm.persistence.models import (
    Account,
    Activity,
    Contact,
    Evidence,
    IngestEvent,
    Lead,
    Proposal,
    ProposalItem,
    ProposalVersion,
    ReviewCandidate,
    SourceIdentity,
)


T = TypeVar("T")


class Repository(Generic[T]):
    def __init__(self, session: Session, model: type[T]):
        self.session = session
        self.model = model

    def get(
        self, workspace_id: UUID, row_id: UUID, *, for_update: bool = False
    ) -> T | None:
        statement = select(self.model).where(
            self.model.workspace_id == workspace_id,
            self.model.id == row_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def add(self, row: T) -> T:
        self.session.add(row)
        self.session.flush()
        return row


class AccountRepository(Repository[Account]):
    def __init__(self, session: Session):
        super().__init__(session, Account)

    def by_contact_email(self, workspace_id: UUID, email: str) -> list[Account]:
        statement = (
            select(Account)
            .join(
                Contact,
                (Contact.workspace_id == Account.workspace_id)
                & (Contact.account_id == Account.id),
            )
            .where(Account.workspace_id == workspace_id, Contact.primary_email == email)
        )
        return list(self.session.scalars(statement))

    def by_domain_name(
        self, workspace_id: UUID, domain: str, name: str
    ) -> list[Account]:
        statement = select(Account).where(
            Account.workspace_id == workspace_id,
            Account.primary_domain == domain,
            Account.normalized_name == name,
        )
        return list(self.session.scalars(statement))


class ContactRepository(Repository[Contact]):
    def __init__(self, session: Session):
        super().__init__(session, Contact)

    def by_email(
        self, workspace_id: UUID, email: str, *, for_update: bool = False
    ) -> Contact | None:
        statement = select(Contact).where(
            Contact.workspace_id == workspace_id,
            Contact.primary_email == email,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)


class LeadRepository(Repository[Lead]):
    def __init__(self, session: Session):
        super().__init__(session, Lead)


class ActivityRepository(Repository[Activity]):
    def __init__(self, session: Session):
        super().__init__(session, Activity)

    def replay(
        self, workspace_id: UUID, ingest_event_id: UUID, activity_type: str
    ) -> Activity | None:
        return self.session.scalar(
            select(Activity).where(
                Activity.workspace_id == workspace_id,
                Activity.ingest_event_id == ingest_event_id,
                Activity.activity_type == activity_type,
            )
        )


class ProposalRepository(Repository[Proposal]):
    def __init__(self, session: Session):
        super().__init__(session, Proposal)

    def portfolio_rows(
        self, workspace_id: UUID
    ) -> list[tuple[Proposal, ProposalVersion | None]]:
        statement = (
            select(Proposal, ProposalVersion)
            .join(
                ProposalVersion,
                (ProposalVersion.proposal_id == Proposal.id)
                & (ProposalVersion.id == Proposal.selected_version_id),
                isouter=True,
            )
            .where(Proposal.workspace_id == workspace_id)
        )
        return list(self.session.execute(statement).all())

    def by_thread(
        self,
        workspace_id: UUID,
        thread_source_identity_id: UUID,
        *,
        for_update: bool = False,
    ) -> Proposal | None:
        statement = select(Proposal).where(
            Proposal.workspace_id == workspace_id,
            Proposal.thread_source_identity_id == thread_source_identity_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)


class ProposalVersionRepository(Repository[ProposalVersion]):
    def __init__(self, session: Session):
        super().__init__(session, ProposalVersion)

    def for_proposal(self, proposal_id: UUID) -> list[ProposalVersion]:
        return list(
            self.session.scalars(
                select(ProposalVersion)
                .where(ProposalVersion.proposal_id == proposal_id)
                .order_by(ProposalVersion.version_number)
            )
        )

    def get_for_proposal(
        self, proposal_id: UUID, version_id: UUID
    ) -> ProposalVersion | None:
        return self.session.scalar(
            select(ProposalVersion).where(
                ProposalVersion.proposal_id == proposal_id,
                ProposalVersion.id == version_id,
            )
        )


class ProposalItemRepository(Repository[ProposalItem]):
    def __init__(self, session: Session):
        super().__init__(session, ProposalItem)

    def for_versions(self, version_ids: set[UUID]) -> list[ProposalItem]:
        if not version_ids:
            return []
        return list(
            self.session.scalars(
                select(ProposalItem).where(
                    ProposalItem.proposal_version_id.in_(version_ids)
                )
            )
        )


class SourceIdentityRepository(Repository[SourceIdentity]):
    def __init__(self, session: Session):
        super().__init__(session, SourceIdentity)


class EvidenceRepository(Repository[Evidence]):
    def __init__(self, session: Session):
        super().__init__(session, Evidence)

    def by_source(
        self, workspace_id: UUID, source_identity_id: UUID, content_hash: str
    ) -> Evidence | None:
        return self.session.scalar(
            select(Evidence).where(
                Evidence.workspace_id == workspace_id,
                Evidence.source_identity_id == source_identity_id,
                Evidence.content_hash == content_hash,
            )
        )


class ReviewCandidateRepository(Repository[ReviewCandidate]):
    def __init__(self, session: Session):
        super().__init__(session, ReviewCandidate)

    def open_by_key(
        self, workspace_id: UUID, dedupe_key: str
    ) -> ReviewCandidate | None:
        return self.session.scalar(
            select(ReviewCandidate).where(
                ReviewCandidate.workspace_id == workspace_id,
                ReviewCandidate.dedupe_key == dedupe_key,
                ReviewCandidate.state == "open",
            )
        )


class IngestEventRepository(Repository[IngestEvent]):
    def __init__(self, session: Session):
        super().__init__(session, IngestEvent)
