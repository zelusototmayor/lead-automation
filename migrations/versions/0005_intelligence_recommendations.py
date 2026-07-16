"""Create the separate evidence-backed intelligence workspace.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "recommendations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "state", sa.String(16), server_default=sa.text("'open'"), nullable=False
        ),
        sa.Column("dedupe_key", sa.String(512), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "rule_code IN ('held_meeting_without_notes', 'promised_proposal_not_sent', 'proposal_missing_next_action', 'proposal_stale', 'inbound_awaiting_response', 'meeting_without_calendar_event', 'contradictory_value_status_sources', 'matching_review_candidate', 'value_review_candidate')",
            name="ck_recommendations_rule_code",
        ),
        sa.CheckConstraint(
            "priority IN ('critical', 'high', 'medium', 'low')",
            name="ck_recommendations_priority",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'resolved', 'dismissed')",
            name="ck_recommendations_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) > 0",
            name="ck_recommendations_evidence_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(dedupe_key)) > 0",
            name="ck_recommendations_dedupe_key_nonblank",
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR state <> 'open'",
            name="ck_recommendations_resolution_state",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_recommendations_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["proposals.workspace_id", "proposals.id"],
            name="fk_recommendations_workspace_proposal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recommendations"),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_recommendations_workspace_id"
        ),
    )
    op.create_index(
        "uq_recommendations_open_dedupe",
        "recommendations",
        ["workspace_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_index(
        "ix_recommendations_workspace_priority_created",
        "recommendations",
        ["workspace_id", "priority", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recommendations_workspace_priority_created", table_name="recommendations"
    )
    op.drop_index("uq_recommendations_open_dedupe", table_name="recommendations")
    op.drop_table("recommendations")
