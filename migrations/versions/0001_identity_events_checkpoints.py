"""Create CRM identity, ingestion event, and checkpoint tables.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


SOURCE_SYSTEMS = "'google_sheets', 'gmail', 'google_calendar', 'granola', 'manual', 'agent'"
ENTITY_KINDS = "'lead', 'account', 'contact', 'message', 'thread', 'meeting', 'proposal', 'document'"
PROCESSING_STATUSES = "'received', 'processing', 'applied', 'ignored', 'review', 'failed', 'dead_letter'"


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(64), server_default=sa.text("'Europe/Lisbon'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(slug)) > 0", name="ck_workspaces_slug_nonblank"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_workspaces_name_nonblank"),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    op.create_table(
        "source_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("entity_kind", sa.String(32), nullable=False),
        sa.Column("source_scope", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("canonical_entity_type", sa.String(64), nullable=True),
        sa.Column("canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint(f"source_system IN ({SOURCE_SYSTEMS})", name="ck_source_identities_source_system"),
        sa.CheckConstraint(f"entity_kind IN ({ENTITY_KINDS})", name="ck_source_identities_entity_kind"),
        sa.CheckConstraint("length(btrim(source_scope)) > 0", name="ck_source_identities_source_scope_nonblank"),
        sa.CheckConstraint("length(btrim(external_id)) > 0", name="ck_source_identities_external_id_nonblank"),
        sa.CheckConstraint("first_seen_at <= last_seen_at", name="ck_source_identities_seen_interval"),
        sa.CheckConstraint("(canonical_entity_type IS NULL) = (canonical_entity_id IS NULL)", name="ck_source_identities_canonical_pair"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_source_identities_workspace_id_workspaces", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_source_identities"),
        sa.UniqueConstraint("workspace_id", "source_system", "source_scope", "entity_kind", "external_id", name="uq_source_identities_workspace_source_scope_kind_external"),
    )
    op.create_index("ix_source_identities_canonical_entity", "source_identities", ["canonical_entity_type", "canonical_entity_id"])

    op.create_table(
        "ingest_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("source_scope", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("external_event_id", sa.String(512), nullable=True),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("processing_status", sa.String(32), server_default=sa.text("'received'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_redacted", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(f"source_system IN ({SOURCE_SYSTEMS})", name="ck_ingest_events_source_system"),
        sa.CheckConstraint("length(btrim(source_scope)) > 0", name="ck_ingest_events_source_scope_nonblank"),
        sa.CheckConstraint("length(btrim(event_type)) > 0", name="ck_ingest_events_event_type_nonblank"),
        sa.CheckConstraint("schema_version > 0", name="ck_ingest_events_schema_version_positive"),
        sa.CheckConstraint("length(btrim(idempotency_key)) > 0", name="ck_ingest_events_idempotency_key_nonblank"),
        sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="ck_ingest_events_payload_hash_sha256"),
        sa.CheckConstraint(f"processing_status IN ({PROCESSING_STATUSES})", name="ck_ingest_events_processing_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_ingest_events_attempt_count_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_ingest_events_workspace_id_workspaces", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_events"),
        sa.UniqueConstraint("workspace_id", "source_system", "idempotency_key", name="uq_ingest_events_workspace_source_key"),
    )
    op.create_index("ix_ingest_events_processing_next_attempt", "ingest_events", ["processing_status", "next_attempt_at"])
    op.create_index("ix_ingest_events_workspace_occurred_at", "ingest_events", ["workspace_id", "occurred_at"])

    op.create_table(
        "sync_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column("source_scope", sa.String(255), nullable=False),
        sa.Column("stream", sa.String(128), nullable=False),
        sa.Column("cursor_encrypted", sa.Text(), nullable=True),
        sa.Column("high_watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_redacted", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(connector)) > 0", name="ck_sync_checkpoints_connector_nonblank"),
        sa.CheckConstraint("length(btrim(source_scope)) > 0", name="ck_sync_checkpoints_source_scope_nonblank"),
        sa.CheckConstraint("length(btrim(stream)) > 0", name="ck_sync_checkpoints_stream_nonblank"),
        sa.CheckConstraint("(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="ck_sync_checkpoints_lease_pair"),
        sa.CheckConstraint("consecutive_failures >= 0", name="ck_sync_checkpoints_failures_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_sync_checkpoints_workspace_id_workspaces", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_sync_checkpoints"),
        sa.UniqueConstraint("workspace_id", "connector", "source_scope", "stream", name="uq_sync_checkpoints_workspace_connector_scope_stream"),
    )
    op.create_index("ix_sync_checkpoints_lease_expires_at", "sync_checkpoints", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sync_checkpoints_lease_expires_at", table_name="sync_checkpoints")
    op.drop_table("sync_checkpoints")
    op.drop_index("ix_ingest_events_workspace_occurred_at", table_name="ingest_events")
    op.drop_index("ix_ingest_events_processing_next_attempt", table_name="ingest_events")
    op.drop_table("ingest_events")
    op.drop_index("ix_source_identities_canonical_entity", table_name="source_identities")
    op.drop_table("source_identities")
    op.drop_table("workspaces")
