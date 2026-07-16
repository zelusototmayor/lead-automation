"""Add transactional human-command outbox and append-only audit.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status", sa.String(16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(btrim(event_type)) > 0", name="ck_outbox_event_type_nonblank"
        ),
        sa.CheckConstraint(
            "length(btrim(aggregate_type)) > 0",
            name="ck_outbox_aggregate_type_nonblank",
        ),
        sa.CheckConstraint(
            "semantic_hash ~ '^[0-9a-f]{64}$'", name="ck_outbox_semantic_hash"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')",
            name="ck_outbox_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_outbox_attempt_count_nonnegative"
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 4096", name="ck_outbox_payload_bounded"
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR status = 'published'",
            name="ck_outbox_published_state",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_outbox_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "command_id", name="uq_outbox_workspace_command"
        ),
    )
    op.create_index("ix_outbox_pending", "outbox_events", ["status", "created_at"])
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(action)) > 0", name="ck_audit_action_nonblank"
        ),
        sa.CheckConstraint(
            "length(btrim(entity_type)) > 0", name="ck_audit_entity_type_nonblank"
        ),
        sa.CheckConstraint(
            "octet_length(details::text) <= 4096", name="ck_audit_details_bounded"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_audit_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "command_id", name="uq_audit_workspace_command"
        ),
    )
    op.create_index(
        "ix_audit_workspace_created", "audit_events", ["workspace_id", "created_at"]
    )
    op.execute("""
        CREATE FUNCTION reject_audit_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit events are append-only' USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION reject_audit_event_mutation()")
    op.drop_index("ix_audit_workspace_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_outbox_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
