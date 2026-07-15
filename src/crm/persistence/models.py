"""PostgreSQL ORM models for CRM identity, ingestion, and checkpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.crm.persistence.base import Base


SOURCE_SYSTEMS = ("google_sheets", "gmail", "google_calendar", "granola", "manual", "agent")
ENTITY_KINDS = ("lead", "account", "contact", "message", "thread", "meeting", "proposal", "document")
PROCESSING_STATUSES = ("received", "processing", "applied", "ignored", "review", "failed", "dead_letter")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("length(btrim(slug)) > 0", name="ck_workspaces_slug_nonblank"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_workspaces_name_nonblank"),
        UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Europe/Lisbon'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class SourceIdentity(Base):
    __tablename__ = "source_identities"
    __table_args__ = (
        CheckConstraint(_in_check("source_system", SOURCE_SYSTEMS), name="ck_source_identities_source_system"),
        CheckConstraint(_in_check("entity_kind", ENTITY_KINDS), name="ck_source_identities_entity_kind"),
        CheckConstraint("length(btrim(source_scope)) > 0", name="ck_source_identities_source_scope_nonblank"),
        CheckConstraint("length(btrim(external_id)) > 0", name="ck_source_identities_external_id_nonblank"),
        CheckConstraint("first_seen_at <= last_seen_at", name="ck_source_identities_seen_interval"),
        CheckConstraint(
            "(canonical_entity_type IS NULL) = (canonical_entity_id IS NULL)",
            name="ck_source_identities_canonical_pair",
        ),
        UniqueConstraint(
            "workspace_id", "source_system", "source_scope", "entity_kind", "external_id",
            name="uq_source_identities_workspace_source_scope_kind_external",
        ),
        Index("ix_source_identities_canonical_entity", "canonical_entity_type", "canonical_entity_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_entity_type: Mapped[str | None] = mapped_column(String(64))
    canonical_entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class IngestEvent(Base):
    __tablename__ = "ingest_events"
    __table_args__ = (
        CheckConstraint(_in_check("source_system", SOURCE_SYSTEMS), name="ck_ingest_events_source_system"),
        CheckConstraint("length(btrim(source_scope)) > 0", name="ck_ingest_events_source_scope_nonblank"),
        CheckConstraint("length(btrim(event_type)) > 0", name="ck_ingest_events_event_type_nonblank"),
        CheckConstraint("schema_version > 0", name="ck_ingest_events_schema_version_positive"),
        CheckConstraint("length(btrim(idempotency_key)) > 0", name="ck_ingest_events_idempotency_key_nonblank"),
        CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="ck_ingest_events_payload_hash_sha256"),
        CheckConstraint(_in_check("processing_status", PROCESSING_STATUSES), name="ck_ingest_events_processing_status"),
        CheckConstraint("attempt_count >= 0", name="ck_ingest_events_attempt_count_nonnegative"),
        UniqueConstraint("workspace_id", "source_system", "idempotency_key", name="uq_ingest_events_workspace_source_key"),
        Index("ix_ingest_events_processing_next_attempt", "processing_status", "next_attempt_at"),
        Index("ix_ingest_events_workspace_occurred_at", "workspace_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(String(512))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'received'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error_redacted: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    causation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"
    __table_args__ = (
        CheckConstraint("length(btrim(connector)) > 0", name="ck_sync_checkpoints_connector_nonblank"),
        CheckConstraint("length(btrim(source_scope)) > 0", name="ck_sync_checkpoints_source_scope_nonblank"),
        CheckConstraint("length(btrim(stream)) > 0", name="ck_sync_checkpoints_stream_nonblank"),
        CheckConstraint("(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="ck_sync_checkpoints_lease_pair"),
        CheckConstraint("consecutive_failures >= 0", name="ck_sync_checkpoints_failures_nonnegative"),
        UniqueConstraint("workspace_id", "connector", "source_scope", "stream", name="uq_sync_checkpoints_workspace_connector_scope_stream"),
        Index("ix_sync_checkpoints_lease_expires_at", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False)
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    stream: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor_encrypted: Mapped[str | None] = mapped_column(Text)
    high_watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_redacted: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
