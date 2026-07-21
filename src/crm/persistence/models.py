"""PostgreSQL ORM models for CRM identity, ingestion, and checkpoints."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CHAR,
    CheckConstraint,
    Date,
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
    "mailbox",
    "thread",
    "meeting",
    "proposal",
    "document",
)
EVIDENCE_TYPES = (
    "sheet_cell",
    "email_message",
    "attachment",
    "calendar_event",
    "meeting_note",
    "manual_confirmation",
    "contract",
    "payment",
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
OUTBOX_STATUSES = ("pending", "publishing", "published", "failed")


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


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(event_type)) > 0", name="ck_outbox_event_type_nonblank"
        ),
        CheckConstraint(
            "length(btrim(aggregate_type)) > 0",
            name="ck_outbox_aggregate_type_nonblank",
        ),
        CheckConstraint(
            "semantic_hash ~ '^[0-9a-f]{64}$'", name="ck_outbox_semantic_hash"
        ),
        CheckConstraint(_in_check("status", OUTBOX_STATUSES), name="ck_outbox_status"),
        CheckConstraint(
            "attempt_count >= 0", name="ck_outbox_attempt_count_nonnegative"
        ),
        CheckConstraint(
            "octet_length(payload::text) <= 4096", name="ck_outbox_payload_bounded"
        ),
        CheckConstraint(
            "published_at IS NULL OR status = 'published'",
            name="ck_outbox_published_state",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_outbox_workspace_id"),
        UniqueConstraint(
            "workspace_id", "command_id", name="uq_outbox_workspace_command"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        Index("ix_outbox_pending", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    command_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(btrim(action)) > 0", name="ck_audit_action_nonblank"),
        CheckConstraint(
            "length(btrim(entity_type)) > 0", name="ck_audit_entity_type_nonblank"
        ),
        CheckConstraint(
            "octet_length(details::text) <= 4096", name="ck_audit_details_bounded"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_audit_workspace_id"),
        UniqueConstraint(
            "workspace_id", "command_id", name="uq_audit_workspace_command"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        Index("ix_audit_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    command_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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
        UniqueConstraint(
            "workspace_id",
            "id",
            "source_system",
            "entity_kind",
            name="uq_source_identities_workspace_id_semantics",
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


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(connector)) > 0",
            name="ck_reconciliation_runs_connector_nonblank",
        ),
        CheckConstraint(
            "length(btrim(source_scope)) > 0",
            name="ck_reconciliation_runs_source_scope_nonblank",
        ),
        CheckConstraint(
            "window_end_at >= window_start_at",
            name="ck_reconciliation_runs_window_order",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_reconciliation_runs_finish_order",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_reconciliation_runs_status",
        ),
        CheckConstraint(
            "((status = 'running' AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)) IS TRUE",
            name="ck_reconciliation_runs_finish_state",
        ),
        CheckConstraint(
            "scanned_count >= 0 AND created_count >= 0 AND updated_count >= 0 "
            "AND duplicate_count >= 0 AND conflict_count >= 0 AND error_count >= 0",
            name="ck_reconciliation_runs_counts_nonnegative",
        ),
        CheckConstraint(
            "jsonb_typeof(report) = 'object' AND octet_length(report::text) <= 512 "
            "AND (report - ARRAY['duplicate', 'conflict', 'error', 'unmapped', 'missing']) = '{}'::jsonb "
            "AND NOT jsonb_path_exists(report, '$.* ? (@.type() != \"number\" || @ < 0)')",
            name="ck_reconciliation_runs_report_minimized",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_reconciliation_runs_workspace_id"
        ),
        Index(
            "ix_reconciliation_runs_workspace_connector_started",
            "workspace_id",
            "connector",
            "started_at",
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
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    window_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scanned_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    duplicate_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    conflict_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
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
            "outcome_code IS NULL OR length(btrim(outcome_code)) > 0",
            name="ck_activities_outcome_code_nonblank",
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
        CheckConstraint(
            "(from_stage IS NULL AND to_stage IS NULL) OR "
            "(from_stage IS NOT NULL AND to_stage IS NOT NULL)",
            name="ck_activities_stage_transition_pair",
        ),
        CheckConstraint(
            "activity_type = 'stage_change' OR "
            "(from_stage IS NULL AND to_stage IS NULL)",
            name="ck_activities_stage_transition_type",
        ),
        CheckConstraint(
            "(from_stage IS NULL AND to_stage IS NULL) OR "
            f"({_in_check('from_stage', LEAD_STAGES)} AND "
            f"{_in_check('to_stage', LEAD_STAGES)} AND from_stage <> to_stage)",
            name="ck_activities_stage_transition_values",
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
    outcome_code: Mapped[str | None] = mapped_column(String(32))
    from_stage: Mapped[str | None] = mapped_column(String(32))
    to_stage: Mapped[str | None] = mapped_column(String(32))
    source_system: Mapped[str | None] = mapped_column(String(32))
    source_identity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    ingest_event_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_type: Mapped[str | None] = mapped_column(String(64))
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    supersedes_activity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_email_messages_direction"
        ),
        CheckConstraint(
            "length(btrim(provider_message_id)) > 0",
            name="ck_email_messages_provider_message_id_nonblank",
        ),
        CheckConstraint(
            "length(btrim(provider_thread_id)) > 0",
            name="ck_email_messages_provider_thread_id_nonblank",
        ),
        CheckConstraint(
            "mailbox_source_system = 'gmail' AND mailbox_entity_kind = 'mailbox'",
            name="ck_email_messages_mailbox_identity",
        ),
        CheckConstraint(
            "from_address IS NULL OR octet_length(from_address::text) <= 320",
            name="ck_email_messages_from_address_bounded",
        ),
        CheckConstraint(
            "jsonb_typeof(to_addresses) = 'array' "
            "AND octet_length(to_addresses::text) <= 4096 "
            "AND NOT jsonb_path_exists(to_addresses, '$[*] ? (@.type() != \"string\")') "
            "AND NOT jsonb_path_exists(to_addresses, "
            '\'$[*] ? (@ like_regex "^\\\\s*$" || @ like_regex "^.{254}.+$" flag "s")\')',
            name="ck_email_messages_to_addresses_minimized",
        ),
        CheckConstraint(
            "evidence_type = 'email_message'",
            name="ck_email_messages_evidence_type",
        ),
        CheckConstraint(
            "subject IS NULL OR octet_length(subject) <= 512",
            name="ck_email_messages_subject_bounded",
        ),
        CheckConstraint(
            "body_preview_redacted IS NULL OR octet_length(body_preview_redacted) <= 2048",
            name="ck_email_messages_body_preview_bounded",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_email_messages_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "mailbox_identity_id",
            "provider_message_id",
            name="uq_email_messages_workspace_mailbox_provider",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_email_messages_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "contact_id"],
            ["contacts.workspace_id", "contacts.account_id", "contacts.id"],
            name="fk_email_messages_workspace_account_contact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "mailbox_identity_id",
                "mailbox_source_system",
                "mailbox_entity_kind",
            ],
            [
                "source_identities.workspace_id",
                "source_identities.id",
                "source_identities.source_system",
                "source_identities.entity_kind",
            ],
            name="fk_email_messages_workspace_mailbox_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "evidence_id", "evidence_type"],
            [
                "evidence.workspace_id",
                "evidence.account_id",
                "evidence.id",
                "evidence.evidence_type",
            ],
            name="fk_email_messages_workspace_account_evidence",
            ondelete="RESTRICT",
        ),
        Index("ix_email_messages_account_sent_at", "account_id", "sent_at"),
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
    contact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    mailbox_identity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    mailbox_source_system: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'gmail'")
    )
    mailbox_entity_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'mailbox'")
    )
    provider_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    provider_thread_id: Mapped[str] = mapped_column(String(512), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_address: Mapped[str | None] = mapped_column(CITEXT())
    to_addresses: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    subject: Mapped[str | None] = mapped_column(Text)
    body_preview_redacted: Mapped[str | None] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    proposal_candidate_state: Mapped[str | None] = mapped_column(String(32))
    evidence_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    evidence_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'email_message'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('booked', 'held', 'cancelled', 'no_show')",
            name="ck_meetings_status",
        ),
        CheckConstraint(
            "(status = 'held' AND held_at IS NOT NULL) OR "
            "(status <> 'held' AND held_at IS NULL)",
            name="ck_meetings_held_state",
        ),
        CheckConstraint(
            "scheduled_end_at IS NULL OR scheduled_end_at >= scheduled_start_at",
            name="ck_meetings_schedule_order",
        ),
        CheckConstraint(
            "(summary IS NULL OR octet_length(summary) <= 4096) "
            "AND (needs IS NULL OR octet_length(needs) <= 4096) "
            "AND (objections IS NULL OR octet_length(objections) <= 4096) "
            "AND (decisions IS NULL OR octet_length(decisions) <= 4096) "
            "AND (commitments IS NULL OR octet_length(commitments) <= 4096)",
            name="ck_meetings_text_minimized",
        ),
        CheckConstraint(
            "jsonb_typeof(next_steps) = 'object' "
            "AND octet_length(next_steps::text) <= 4096 "
            "AND (next_steps - ARRAY['action', 'owner', 'due_at', 'status']) = '{}'::jsonb "
            'AND NOT jsonb_path_exists(next_steps, \'$.* ? (@.type() != "string" && @.type() != "null")\')',
            name="ck_meetings_next_steps_minimized",
        ),
        CheckConstraint(
            "notes_evidence_type = 'meeting_note'",
            name="ck_meetings_notes_evidence_type",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_meetings_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "provider",
            "calendar_id",
            "external_event_id",
            "occurrence_start_at",
            name="uq_meetings_workspace_provider_occurrence",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_meetings_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "lead_id"],
            ["leads.workspace_id", "leads.id"],
            name="fk_meetings_workspace_lead",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "lead_id"],
            ["leads.workspace_id", "leads.account_id", "leads.id"],
            name="fk_meetings_workspace_account_lead",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "notes_evidence_id", "notes_evidence_type"],
            [
                "evidence.workspace_id",
                "evidence.account_id",
                "evidence.id",
                "evidence.evidence_type",
            ],
            name="fk_meetings_workspace_account_notes_evidence",
            ondelete="RESTRICT",
        ),
        Index("ix_meetings_account_scheduled", "account_id", "scheduled_start_at"),
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
    lead_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(512), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(512), nullable=False)
    occurrence_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    needs: Mapped[str | None] = mapped_column(Text)
    objections: Mapped[str | None] = mapped_column(Text)
    decisions: Mapped[str | None] = mapped_column(Text)
    commitments: Mapped[str | None] = mapped_column(Text)
    next_steps: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    notes_evidence_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    notes_evidence_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'meeting_note'")
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


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(task_type)) > 0", name="ck_tasks_task_type_nonblank"
        ),
        CheckConstraint(
            "(task_type NOT IN ('call', 'email') OR lead_id IS NOT NULL) IS TRUE",
            name="ck_tasks_lead_context",
        ),
        CheckConstraint(
            "account_id IS NOT NULL OR lead_id IS NOT NULL",
            name="ck_tasks_requires_account_or_lead",
        ),
        CheckConstraint(
            "proposal_id IS NULL OR account_id IS NOT NULL",
            name="ck_tasks_proposal_requires_account",
        ),
        CheckConstraint("length(btrim(title)) > 0", name="ck_tasks_title_nonblank"),
        CheckConstraint("octet_length(title) <= 512", name="ck_tasks_title_bounded"),
        CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')", name="ck_tasks_status"
        ),
        CheckConstraint(
            "((status = 'completed' AND completed_at IS NOT NULL "
            "AND completion_activity_id IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL "
            "AND completion_activity_id IS NULL)) IS TRUE",
            name="ck_tasks_completion_state",
        ),
        CheckConstraint(
            "source_rule IS NULL OR length(btrim(source_rule)) > 0",
            name="ck_tasks_source_rule_nonblank",
        ),
        CheckConstraint("version > 0", name="ck_tasks_version_positive"),
        UniqueConstraint("workspace_id", "id", name="uq_tasks_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_tasks_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "proposal_id"],
            ["proposals.workspace_id", "proposals.account_id", "proposals.id"],
            name="fk_tasks_workspace_account_proposal",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "lead_id"],
            ["leads.workspace_id", "leads.account_id", "leads.id"],
            name="fk_tasks_workspace_account_lead",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "lead_id"],
            ["leads.workspace_id", "leads.id"],
            name="fk_tasks_workspace_lead",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "completion_activity_id"],
            ["activities.workspace_id", "activities.account_id", "activities.id"],
            name="fk_tasks_workspace_account_completion_activity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "completion_activity_id"],
            ["activities.workspace_id", "activities.id"],
            name="fk_tasks_workspace_completion_activity",
            ondelete="RESTRICT",
        ),
        Index("ix_tasks_account_status_due", "account_id", "status", "due_at"),
        Index(
            "ix_tasks_workspace_lead_status_due",
            "workspace_id",
            "lead_id",
            "status",
            "due_at",
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
    proposal_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'open'")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_activity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_rule: Mapped[str | None] = mapped_column(String(128))
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


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            _in_check("evidence_type", EVIDENCE_TYPES), name="ck_evidence_type"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_evidence_content_hash_sha256"
        ),
        CheckConstraint(
            "uri IS NULL OR length(btrim(uri)) > 0", name="ck_evidence_uri_nonblank"
        ),
        CheckConstraint(
            "excerpt_redacted IS NULL OR length(btrim(excerpt_redacted)) > 0",
            name="ck_evidence_excerpt_nonblank",
        ),
        CheckConstraint(
            "length(btrim(sensitivity)) > 0", name="ck_evidence_sensitivity_nonblank"
        ),
        CheckConstraint(
            "retention_until IS NULL OR retention_until >= captured_at",
            name="ck_evidence_retention_interval",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_evidence_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            "evidence_type",
            name="uq_evidence_workspace_account_id_type",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_identity_id",
            "content_hash",
            name="uq_evidence_workspace_source_hash",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_evidence_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_evidence_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        Index("ix_evidence_account_captured_at", "account_id", "captured_at"),
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
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_identity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    uri: Mapped[str | None] = mapped_column(Text)
    excerpt_redacted: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    sensitivity: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'confidential'")
    )
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewCandidate(Base):
    __tablename__ = "review_candidates"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('send_promised_proposal', 'review_proposal_value')",
            name="ck_review_candidates_action_type",
        ),
        CheckConstraint(
            "state IN ('open', 'resolved', 'dismissed')",
            name="ck_review_candidates_state",
        ),
        CheckConstraint(
            "length(btrim(dedupe_key)) > 0",
            name="ck_review_candidates_dedupe_key_nonblank",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR state <> 'open'",
            name="ck_review_candidates_resolution_state",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_review_candidates_workspace_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_review_candidates_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["evidence.workspace_id", "evidence.id"],
            name="fk_review_candidates_workspace_evidence",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["proposals.workspace_id", "proposals.id"],
            name="fk_review_candidates_workspace_proposal",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_review_candidates_open_dedupe",
            "workspace_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("state = 'open'"),
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
    proposal_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'open'")
    )
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


RECOMMENDATION_RULE_CODES = (
    "held_meeting_without_notes",
    "promised_proposal_not_sent",
    "proposal_missing_next_action",
    "proposal_stale",
    "inbound_awaiting_response",
    "meeting_without_calendar_event",
    "contradictory_value_status_sources",
    "matching_review_candidate",
    "value_review_candidate",
)
RECOMMENDATION_PRIORITIES = ("critical", "high", "medium", "low")
RECOMMENDATION_STATES = ("open", "resolved", "dismissed")


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(
            _in_check("rule_code", RECOMMENDATION_RULE_CODES),
            name="ck_recommendations_rule_code",
        ),
        CheckConstraint(
            _in_check("priority", RECOMMENDATION_PRIORITIES),
            name="ck_recommendations_priority",
        ),
        CheckConstraint(
            _in_check("state", RECOMMENDATION_STATES),
            name="ck_recommendations_state",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) > 0",
            name="ck_recommendations_evidence_nonempty",
        ),
        CheckConstraint(
            "length(btrim(dedupe_key)) > 0",
            name="ck_recommendations_dedupe_key_nonblank",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR state <> 'open'",
            name="ck_recommendations_resolution_state",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_recommendations_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_recommendations_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["proposals.workspace_id", "proposals.id"],
            name="fk_recommendations_workspace_proposal",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_recommendations_open_dedupe",
            "workspace_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
        Index(
            "ix_recommendations_workspace_priority_created",
            "workspace_id",
            "priority",
            "created_at",
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
    proposal_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_json: Mapped[list[str]] = mapped_column("evidence", JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'open'")
    )
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


PROPOSAL_STATUSES = (
    "draft",
    "promised",
    "sent",
    "viewed",
    "negotiation",
    "won",
    "lost",
    "withdrawn",
    "expired",
)
PROPOSAL_SENT_OR_LATER_STATUSES = (
    "sent",
    "viewed",
    "negotiation",
    "won",
    "lost",
    "withdrawn",
    "expired",
)
PROPOSAL_VERSION_STATUSES = ("draft", "sent", "superseded", "accepted", "rejected")


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (
        CheckConstraint(
            _in_check("status", PROPOSAL_STATUSES), name="ck_proposals_status"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_proposals_currency_iso"),
        CheckConstraint(
            "probability IS NULL OR probability BETWEEN 0 AND 100",
            name="ck_proposals_probability",
        ),
        CheckConstraint(
            "value_state IN ('missing', 'candidate', 'confirmed', 'rejected')",
            name="ck_proposals_value_state",
        ),
        CheckConstraint(
            "sent_verification_state IS NULL OR "
            "sent_verification_state IN ('verified', 'legacy_unverified')",
            name="ck_proposals_sent_verification_state",
        ),
        CheckConstraint(
            f"(status IN ({', '.join(repr(value) for value in PROPOSAL_SENT_OR_LATER_STATUSES)}) "
            "AND sent_at IS NOT NULL AND sent_verification_state IS NOT NULL AND "
            "((sent_verification_state = 'verified' AND sent_evidence_id IS NOT NULL) OR "
            "sent_verification_state = 'legacy_unverified')) "
            f"OR (status NOT IN ({', '.join(repr(value) for value in PROPOSAL_SENT_OR_LATER_STATUSES)}) "
            "AND sent_at IS NULL AND sent_evidence_id IS NULL "
            "AND sent_verification_state IS NULL)",
            name="ck_proposals_sent_evidence",
        ),
        CheckConstraint("length(btrim(title)) > 0", name="ck_proposals_title_nonblank"),
        CheckConstraint(
            "proposal_number IS NULL OR length(btrim(proposal_number)) > 0",
            name="ck_proposals_number_nonblank",
        ),
        CheckConstraint(
            "probability_source IS NULL OR length(btrim(probability_source)) > 0",
            name="ck_proposals_probability_source_nonblank",
        ),
        CheckConstraint(
            "forecast_category IS NULL OR length(btrim(forecast_category)) > 0",
            name="ck_proposals_forecast_category_nonblank",
        ),
        CheckConstraint(
            "next_action IS NULL OR length(btrim(next_action)) > 0",
            name="ck_proposals_next_action_nonblank",
        ),
        CheckConstraint(
            "next_action_due_at IS NULL OR next_action IS NOT NULL",
            name="ck_proposals_next_action_due_requires_action",
        ),
        CheckConstraint(
            "lost_reason IS NULL OR length(btrim(lost_reason)) > 0",
            name="ck_proposals_lost_reason_nonblank",
        ),
        CheckConstraint(
            "(status = 'won' AND won_at IS NOT NULL AND lost_at IS NULL AND lost_reason IS NULL) "
            "OR (status = 'lost' AND won_at IS NULL AND lost_at IS NOT NULL AND lost_reason IS NOT NULL) "
            "OR (status NOT IN ('won', 'lost') AND won_at IS NULL AND lost_at IS NULL AND lost_reason IS NULL)",
            name="ck_proposals_close_state",
        ),
        CheckConstraint("version > 0", name="ck_proposals_version_positive"),
        UniqueConstraint("workspace_id", "id", name="uq_proposals_workspace_id"),
        UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_proposals_workspace_account_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_proposals_workspace_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "lead_id"],
            ["leads.workspace_id", "leads.account_id", "leads.id"],
            name="fk_proposals_workspace_account_lead",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["id", "selected_version_id"],
            ["proposal_versions.proposal_id", "proposal_versions.id"],
            name="fk_proposals_selected_version",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index("ix_proposals_account_id", "account_id"),
        Index("ix_proposals_lead_id", "lead_id"),
        Index(
            "uq_proposals_workspace_thread",
            "workspace_id",
            "thread_source_identity_id",
            unique=True,
            postgresql_where=text("thread_source_identity_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_proposals_workspace_thread_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "sent_evidence_id"],
            ["evidence.workspace_id", "evidence.id"],
            name="fk_proposals_workspace_sent_evidence",
            ondelete="RESTRICT",
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
    lead_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    thread_source_identity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    proposal_number: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'draft'")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_evidence_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    sent_verification_state: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    probability_source: Mapped[str | None] = mapped_column(String(64))
    forecast_category: Mapped[str | None] = mapped_column(String(64))
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(Text)
    selected_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    value_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'missing'")
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


class ProposalVersion(Base):
    __tablename__ = "proposal_versions"
    __table_args__ = (
        CheckConstraint(
            "version_number > 0", name="ck_proposal_versions_version_number_positive"
        ),
        CheckConstraint(
            _in_check("status", PROPOSAL_VERSION_STATUSES),
            name="ck_proposal_versions_status",
        ),
        CheckConstraint(
            "tax_inclusion IN ('exclusive', 'inclusive', 'unknown')",
            name="ck_proposal_versions_tax_inclusion",
        ),
        CheckConstraint(
            "one_off_amount IS NULL OR one_off_amount >= 0",
            name="ck_proposal_versions_one_off_nonnegative",
        ),
        CheckConstraint(
            "mrr_amount IS NULL OR mrr_amount >= 0",
            name="ck_proposal_versions_mrr_nonnegative",
        ),
        CheckConstraint(
            "arr_amount IS NULL OR arr_amount >= 0",
            name="ck_proposal_versions_arr_nonnegative",
        ),
        CheckConstraint(
            "extraction_confidence IS NULL OR extraction_confidence BETWEEN 0 AND 1",
            name="ck_proposal_versions_extraction_confidence",
        ),
        CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_proposal_versions_confirmation_pair",
        ),
        CheckConstraint(
            "confirmed_by IS NULL OR source_document_evidence_id IS NOT NULL",
            name="ck_proposal_versions_confirmation_evidence",
        ),
        CheckConstraint(
            "confirmed_by IS NULL OR one_off_amount IS NOT NULL "
            "OR mrr_amount IS NOT NULL OR arr_amount IS NOT NULL",
            name="ck_proposal_versions_confirmation_value",
        ),
        CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL",
            name="ck_proposal_versions_sent_at",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until >= created_at::date",
            name="ck_proposal_versions_valid_interval",
        ),
        UniqueConstraint(
            "proposal_id",
            "version_number",
            name="uq_proposal_versions_proposal_version_number",
        ),
        UniqueConstraint("proposal_id", "id", name="uq_proposal_versions_proposal_id"),
        Index("ix_proposal_versions_proposal_id", "proposal_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    proposal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("proposals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'draft'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[date | None] = mapped_column(Date)
    one_off_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    mrr_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    arr_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    tax_inclusion: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'unknown'")
    )
    source_document_evidence_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("evidence.id", ondelete="RESTRICT")
    )
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    confirmed_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProposalItem(Base):
    __tablename__ = "proposal_items"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_proposal_items_description_nonblank",
        ),
        CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name="ck_proposal_items_quantity_positive",
        ),
        CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="ck_proposal_items_unit_price_nonnegative",
        ),
        CheckConstraint(
            "amount IS NULL OR amount >= 0", name="ck_proposal_items_amount_nonnegative"
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_proposal_items_currency_iso"
        ),
        CheckConstraint(
            "billing_period IS NULL OR billing_period IN ('mrr', 'arr')",
            name="ck_proposal_items_billing_period",
        ),
        CheckConstraint(
            "option_group IS NULL OR length(btrim(option_group)) > 0",
            name="ck_proposal_items_option_group_nonblank",
        ),
        Index("ix_proposal_items_proposal_version_id", "proposal_version_id"),
        Index(
            "uq_proposal_items_selected_option",
            "proposal_version_id",
            "option_group",
            unique=True,
            postgresql_where=text("is_selected AND option_group IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    proposal_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("proposal_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    billing_period: Mapped[str | None] = mapped_column(String(32))
    option_group: Mapped[str | None] = mapped_column(Text)
    is_selected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)


class ProposalFollowup(Base):
    __tablename__ = "proposal_followups"
    __table_args__ = (
        CheckConstraint(
            "sequence_number > 0", name="ck_proposal_followups_sequence_positive"
        ),
        CheckConstraint(
            "length(btrim(channel)) > 0",
            name="ck_proposal_followups_channel_nonblank",
        ),
        UniqueConstraint("activity_id", name="uq_proposal_followups_activity"),
        UniqueConstraint(
            "proposal_id", "sequence_number", name="uq_proposal_followups_sequence"
        ),
        Index("ix_proposal_followups_proposal_id", "proposal_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    proposal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("proposals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    activity_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("activities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)


def _reject_activity_mutation(*_args: object, **_kwargs: object) -> None:
    raise ValueError("activities are immutable")


event.listen(Activity, "before_update", _reject_activity_mutation)
event.listen(Activity, "before_delete", _reject_activity_mutation)
event.listen(Evidence, "before_update", _reject_activity_mutation)
event.listen(Evidence, "before_delete", _reject_activity_mutation)
event.listen(AuditEvent, "before_update", _reject_activity_mutation)
event.listen(AuditEvent, "before_delete", _reject_activity_mutation)
