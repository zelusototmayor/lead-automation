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
    IngestEvent,
    Lead,
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


class SourceIdentityRepository(Repository[SourceIdentity]):
    def __init__(self, session: Session):
        super().__init__(session, SourceIdentity)


class IngestEventRepository(Repository[IngestEvent]):
    def __init__(self, session: Session):
        super().__init__(session, IngestEvent)
