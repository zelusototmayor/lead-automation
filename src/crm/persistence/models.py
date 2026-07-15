"""PostgreSQL ORM models for CRM identity, ingestion, and checkpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.crm.persistence.base import Base


SOURCE_SYSTEMS = (
    "google_sheets",
    "gmail",
    "google_calendar",
    "granola",
    "manual",
    "agent",
)
ENTITY_KINDS = (
    "lead",
    "account",
    "contact",
    "message",
    "thread",
    "meeting",
    "proposal",
    "document",
)
PROCESSING_STATUSES = (
    "received",
    "processing",
    "applied",
    "ignored",
    "review",
    "failed",
    "dead_letter",
)


def _in_check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("length(btrim(slug)) > 0", name="ck_workspaces_slug_nonblank"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_workspaces_name_nonblank"),
        UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'Europe/Lisbon'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SourceIdentity(Base):
    __tablename__ = "source_identities"
    __table_args__ = (
        CheckConstraint(
            _in_check("source_system", SOURCE_SYSTEMS),
            name="ck_source_identities_source_system",
        ),
        CheckConstraint(
            _in_check("entity_kind", ENTITY_KINDS),
            name="ck_source_identities_entity_kind",
        ),
        CheckConstraint(
            "length(btrim(source_scope)) > 0",
            name="ck_source_identities_source_scope_nonblank",
        ),
        CheckConstraint(
            "length(btrim(external_id)) > 0",
            name="ck_source_identities_external_id_nonblank",
        ),
        CheckConstraint(
            "first_seen_at <= last_seen_at", name="ck_source_identities_seen_interval"
        ),
        CheckConstraint(
            "(canonical_entity_type IS NULL) = (canonical_entity_id IS NULL)",
            name="ck_source_identities_canonical_pair",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_system",
            "source_scope",
            "entity_kind",
            "external_id",
            name="uq_source_identities_workspace_source_scope_kind_external",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_source_identities_workspace_id"
        ),
        Index(
            "ix_source_identities_canonical_entity",
            "canonical_entity_type",
            "canonical_entity_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_entity_type: Mapped[str | None] = mapped_column(String(64))
    canonical_entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class IngestEvent(Base):
    __tablename__ = "ingest_events"
    __table_args__ = (
        CheckConstraint(
            _in_check("source_system", SOURCE_SYSTEMS),
            name="ck_ingest_events_source_system",
        ),
        CheckConstraint(
            "length(btrim(source_scope)) > 0",
            name="ck_ingest_events_source_scope_nonblank",
        ),
        CheckConstraint(
            "length(btrim(event_type)) > 0", name="ck_ingest_events_event_type_nonblank"
        ),
        CheckConstraint(
            "schema_version > 0", name="ck_ingest_events_schema_version_positive"
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_ingest_events_idempotency_key_nonblank",
        ),
        CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_ingest_events_payload_hash_sha256",
        ),
        CheckConstraint(
            "stage_reduction_fingerprint IS NULL OR "
            "stage_reduction_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_ingest_events_stage_reduction_fingerprint",
        ),
        CheckConstraint(
            _in_check("processing_status", PROCESSING_STATUSES),
            name="ck_ingest_events_processing_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_ingest_events_attempt_count_nonnegative"
        ),
        UniqueConstraint(
            "workspace_id",
            "source_system",
            "idempotency_key",
            name="uq_ingest_events_workspace_source_key",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_ingest_events_workspace_id"),
        Index(
            "ix_ingest_events_processing_next_attempt",
            "processing_status",
            "next_attempt_at",
        ),
        Index("ix_ingest_events_workspace_occurred_at", "workspace_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(String(512))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_reduction_fingerprint: Mapped[str | None] = mapped_column(String(64))
    processing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'received'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error_redacted: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    causation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(connector)) > 0",
            name="ck_sync_checkpoints_connector_nonblank",
        ),
        CheckConstraint(
            "length(btrim(source_scope)) > 0",
            name="ck_sync_checkpoints_source_scope_nonblank",
        ),
        CheckConstraint(
            "length(btrim(stream)) > 0", name="ck_sync_checkpoints_stream_nonblank"
        ),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_sync_checkpoints_lease_pair",
        ),
        CheckConstraint(
            "consecutive_failures >= 0", name="ck_sync_checkpoints_failures_nonnegative"
        ),
        UniqueConstraint(
            "workspace_id",
            "connector",
            "source_scope",
            "stream",
            name="uq_sync_checkpoints_workspace_connector_scope_stream",
        ),
        Index("ix_sync_checkpoints_lease_expires_at", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    stream: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor_encrypted: Mapped[str | None] = mapped_column(Text)
    high_watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_redacted: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


ACCOUNT_LIFECYCLE_STAGES = (
    "potential",
    "meeting",
    "proposal",
    "customer",
    "lost",
    "inactive",
)
CONTACT_STATUSES = ("active", "inactive")
LEAD_STAGES = (
    "new",
    "contacted",
    "qualified",
    "meeting_booked",
    "meeting_held",
    "proposal_requested",
    "proposal_sent",
    "negotiation",
    "won",
    "lost",
    "not_a_fit",
)
ACTIVITY_TYPES = (
    "stage_change",
    "call",
    "email_sent",
    "email_received",
    "meeting",
    "proposal",
    "note",
    "task",
    "system",
)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(display_name)) > 0", name="ck_accounts_display_name_nonblank"
        ),
        CheckConstraint(
            "length(btrim(normalized_name)) > 0",
            name="ck_accounts_normalized_name_nonblank",
        ),
        CheckConstraint(
            "legal_name IS NULL OR length(btrim(legal_name)) > 0",
            name="ck_accounts_legal_name_nonblank",
        ),
        CheckConstraint(
            "website_url IS NULL OR length(btrim(website_url)) > 0",
            name="ck_accounts_website_url_nonblank",
        ),
        CheckConstraint(
            "primary_domain IS NULL OR length(btrim(primary_domain::text)) > 0",
            name="ck_accounts_primary_domain_nonblank",
        ),
        CheckConstraint(
            "sector IS NULL OR length(btrim(sector)) > 0",
            name="ck_accounts_sector_nonblank",
        ),
        CheckConstraint(
            "commercial_vertical IS NULL OR length(btrim(commercial_vertical)) > 0",
            name="ck_accounts_vertical_nonblank",
        ),
        CheckConstraint(
            "source_origin IS NULL OR length(btrim(source_origin)) > 0",
            name="ck_accounts_source_origin_nonblank",
        ),
        CheckConstraint(
            "merged_into_account_id IS NULL OR merged_into_account_id <> id",
            name="ck_accounts_not_self_merged",
        ),
        CheckConstraint(
            _in_check("lifecycle_stage", ACCOUNT_LIFECYCLE_STAGES),
            name="ck_accounts_lifecycle_stage",
        ),
        CheckConstraint(
            "highest_stage_rank BETWEEN 0 AND 90", name="ck_accounts_highest_stage_rank"
        ),
        CheckConstraint(
            "source_confidence IS NULL OR source_confidence BETWEEN 0 AND 1",
            name="ck_accounts_source_confidence",
        ),
        CheckConstraint("version > 0", name="ck_accounts_version_positive"),
        UniqueConstraint("workspace_id", "id", name="uq_accounts_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "merged_into_account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_accounts_workspace_merged_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_accounts_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_accounts_workspace_normalized_name", "workspace_id", "normalized_name"
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    legal_name: Mapped[str | None] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    website_url: Mapped[str | None] = mapped_column(Text)
    primary_domain: Mapped[str | None] = mapped_column(CITEXT())
    lifecycle_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'potential'")
    )
    highest_stage_rank: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    owner_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    merged_into_account_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    sector: Mapped[str | None] = mapped_column(String(255))
    commercial_vertical: Mapped[str | None] = mapped_column(String(255))
    source_origin: Mapped[str | None] = mapped_column(String(255))
    source_identity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    __mapper_args__ = {"version_id_col": version}


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            _in_check("status", CONTACT_STATUSES), name="ck_contacts_status"
        ),
        CheckConstraint(
            "primary_email IS NULL OR length(btrim(primary_email::text)) > 0",
            name="ck_contacts_primary_email_nonblank",
        ),
        CheckConstraint(
            "full_name IS NULL OR length(btrim(full_name)) > 0",
            name="ck_contacts_full_name_nonblank",
        ),
        CheckConstraint(
            "title IS NULL OR length(btrim(title)) > 0",
            name="ck_contacts_title_nonblank",
        ),
        CheckConstraint(
            "phone IS NULL OR length(btrim(phone)) > 0",
            name="ck_contacts_phone_nonblank",
        ),
        CheckConstraint("version > 0", name="ck_contacts_version_positive"),
        UniqueConstraint("workspace_id", "id", name="uq_contacts_workspace_id"),
        UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_contacts_workspace_account_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_contacts_workspace_account",
            ondelete="RESTRICT",
        ),
        Index("ix_contacts_account_id", "account_id"),
        Index(
            "uq_contacts_workspace_primary_email",
            "workspace_id",
            "primary_email",
            unique=True,
            postgresql_where=text("primary_email IS NOT NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    primary_email: Mapped[str | None] = mapped_column(CITEXT())
    is_primary: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    __mapper_args__ = {"version_id_col": version}


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint(_in_check("stage", LEAD_STAGES), name="ck_leads_stage"),
        CheckConstraint(
            "contact_id IS NULL OR account_id IS NOT NULL",
            name="ck_leads_contact_requires_account",
        ),
        CheckConstraint(
            "source_stage_raw IS NULL OR length(btrim(source_stage_raw)) > 0",
            name="ck_leads_source_stage_raw_nonblank",
        ),
        CheckConstraint(
            "priority IS NULL OR length(btrim(priority)) > 0",
            name="ck_leads_priority_nonblank",
        ),
        CheckConstraint(
            "sector IS NULL OR length(btrim(sector)) > 0",
            name="ck_leads_sector_nonblank",
        ),
        CheckConstraint(
            "commercial_vertical IS NULL OR length(btrim(commercial_vertical)) > 0",
            name="ck_leads_vertical_nonblank",
        ),
        CheckConstraint(
            "source_origin IS NULL OR length(btrim(source_origin)) > 0",
            name="ck_leads_source_origin_nonblank",
        ),
        CheckConstraint(
            "highest_stage_rank BETWEEN 0 AND 90", name="ck_leads_highest_stage_rank"
        ),
        CheckConstraint("version > 0", name="ck_leads_version_positive"),
        UniqueConstraint("workspace_id", "id", name="uq_leads_workspace_id"),
        UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_leads_workspace_account_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_leads_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_leads_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "contact_id"],
            ["contacts.workspace_id", "contacts.account_id", "contacts.id"],
            name="fk_leads_workspace_account_contact",
            ondelete="RESTRICT",
        ),
        Index("ix_leads_account_id", "account_id"),
        Index("ix_leads_contact_id", "contact_id"),
    )
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    contact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_stage_raw: Mapped[str | None] = mapped_column(String(255))
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'new'")
    )
    highest_stage_rank: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    priority: Mapped[str | None] = mapped_column(String(64))
    owner_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    sector: Mapped[str | None] = mapped_column(String(255))
    commercial_vertical: Mapped[str | None] = mapped_column(String(255))
    source_origin: Mapped[str | None] = mapped_column(String(255))
    source_identity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    __mapper_args__ = {"version_id_col": version}


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(
            _in_check("activity_type", ACTIVITY_TYPES),
            name="ck_activities_activity_type",
        ),
        CheckConstraint(
            "length(btrim(title)) > 0", name="ck_activities_title_nonblank"
        ),
        CheckConstraint(
            "summary IS NULL OR length(btrim(summary)) > 0",
            name="ck_activities_summary_nonblank",
        ),
        CheckConstraint(
            "direction IS NULL OR direction IN ('inbound', 'outbound', 'internal')",
            name="ck_activities_direction",
        ),
        CheckConstraint(
            f"source_system IS NULL OR {_in_check('source_system', SOURCE_SYSTEMS)}",
            name="ck_activities_source_system",
        ),
        CheckConstraint(
            "actor_type IS NULL OR length(btrim(actor_type)) > 0",
            name="ck_activities_actor_type_nonblank",
        ),
        CheckConstraint(
            "supersedes_activity_id IS NULL OR supersedes_activity_id <> id",
            name="ck_activities_not_self_superseding",
        ),
        CheckConstraint(
            "account_id IS NOT NULL OR lead_id IS NOT NULL",
            name="ck_activities_requires_entity",
        ),
        CheckConstraint(
            "contact_id IS NULL OR account_id IS NOT NULL",
            name="ck_activities_contact_requires_account",
        ),
        CheckConstraint(
            "(semantic_fingerprint IS NULL OR semantic_fingerprint ~ '^[0-9a-f]{64}$') "
            "AND (activity_type <> 'stage_change' OR semantic_fingerprint IS NOT NULL)",
            name="ck_activities_semantic_fingerprint",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_activities_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_activities_workspace_account_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_activities_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "lead_id"],
            ["leads.workspace_id", "leads.id"],
            name="fk_activities_workspace_lead",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "lead_id"],
            ["leads.workspace_id", "leads.account_id", "leads.id"],
            name="fk_activities_workspace_account_lead",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "contact_id"],
            ["contacts.workspace_id", "contacts.account_id", "contacts.id"],
            name="fk_activities_workspace_account_contact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_activities_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "ingest_event_id"],
            ["ingest_events.workspace_id", "ingest_events.id"],
            name="fk_activities_workspace_ingest_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "supersedes_activity_id"],
            ["activities.workspace_id", "activities.id"],
            name="fk_activities_workspace_supersedes",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "supersedes_activity_id"],
            ["activities.workspace_id", "activities.account_id", "activities.id"],
            name="fk_activities_workspace_account_supersedes",
            ondelete="RESTRICT",
        ),
        Index("ix_activities_account_occurred_at", "account_id", "occurred_at"),
        Index("ix_activities_lead_id", "lead_id"),
        Index(
            "uq_activities_workspace_ingest_type",
            "workspace_id",
            "ingest_event_id",
            "activity_type",
            unique=True,
            postgresql_where=text("ingest_event_id IS NOT NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lead_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    contact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    semantic_fingerprint: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[str | None] = mapped_column(String(32))
    source_system: Mapped[str | None] = mapped_column(String(32))
    source_identity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    ingest_event_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_type: Mapped[str | None] = mapped_column(String(64))
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    supersedes_activity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _reject_activity_mutation(*_args: object, **_kwargs: object) -> None:
    raise ValueError("activities are immutable")


event.listen(Activity, "before_update", _reject_activity_mutation)
event.listen(Activity, "before_delete", _reject_activity_mutation)
