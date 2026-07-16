"""Create append-only evidence and deterministic review candidates.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("source_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("excerpt_redacted", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "sensitivity",
            sa.String(32),
            server_default=sa.text("'confidential'"),
            nullable=False,
        ),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "evidence_type IN ('sheet_cell', 'email_message', 'attachment', 'calendar_event', 'meeting_note', 'manual_confirmation', 'contract', 'payment')",
            name="ck_evidence_type",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_evidence_content_hash_sha256"
        ),
        sa.CheckConstraint(
            "uri IS NULL OR length(btrim(uri)) > 0", name="ck_evidence_uri_nonblank"
        ),
        sa.CheckConstraint(
            "excerpt_redacted IS NULL OR length(btrim(excerpt_redacted)) > 0",
            name="ck_evidence_excerpt_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(sensitivity)) > 0", name="ck_evidence_sensitivity_nonblank"
        ),
        sa.CheckConstraint(
            "retention_until IS NULL OR retention_until >= captured_at",
            name="ck_evidence_retention_interval",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_evidence_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_evidence_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_evidence_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evidence_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_identity_id",
            "content_hash",
            name="uq_evidence_workspace_source_hash",
        ),
    )
    op.create_index(
        "ix_evidence_account_captured_at", "evidence", ["account_id", "captured_at"]
    )
    op.execute("""
        CREATE FUNCTION crm_reject_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'evidence is append-only'; END; $$
    """)
    op.execute(
        "CREATE TRIGGER trg_crm_evidence_append_only BEFORE UPDATE OR DELETE ON evidence FOR EACH ROW EXECUTE FUNCTION crm_reject_evidence_mutation()"
    )

    # Pre-0004 UUIDs were intentionally unconstrained placeholders, not canonical
    # evidence. Preserve the monetary observations as candidates without inventing
    # provenance or retaining a false confirmation/verification claim.
    op.execute(
        "UPDATE proposals SET value_state = 'candidate' WHERE value_state = 'confirmed'"
    )
    op.execute(
        "UPDATE proposal_versions SET source_document_evidence_id = NULL, confirmed_by = NULL, confirmed_at = NULL WHERE source_document_evidence_id IS NOT NULL"
    )
    op.execute(
        "UPDATE proposals SET sent_evidence_id = NULL, sent_verification_state = 'legacy_unverified' WHERE sent_verification_state = 'verified'"
    )

    op.add_column(
        "proposals",
        sa.Column(
            "thread_source_identity_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_proposals_workspace_thread_identity",
        "proposals",
        "source_identities",
        ["workspace_id", "thread_source_identity_id"],
        ["workspace_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_proposals_workspace_sent_evidence",
        "proposals",
        "evidence",
        ["workspace_id", "sent_evidence_id"],
        ["workspace_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_proposals_workspace_thread",
        "proposals",
        ["workspace_id", "thread_source_identity_id"],
        unique=True,
        postgresql_where=sa.text("thread_source_identity_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_proposal_versions_source_document_evidence_id_evidence",
        "proposal_versions",
        "evidence",
        ["source_document_evidence_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute("""
        CREATE FUNCTION crm_validate_proposal_evidence_tenant() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.source_document_evidence_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM evidence e JOIN proposals p ON p.id = NEW.proposal_id
             WHERE e.id = NEW.source_document_evidence_id
               AND e.workspace_id = p.workspace_id AND e.account_id = p.account_id
          ) THEN RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'proposal evidence context mismatch';
          END IF;
          RETURN NEW;
        END; $$
    """)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposal_versions_evidence_tenant AFTER INSERT OR UPDATE ON proposal_versions DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION crm_validate_proposal_evidence_tenant()"
    )

    op.create_table(
        "review_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column(
            "state", sa.String(16), server_default=sa.text("'open'"), nullable=False
        ),
        sa.Column("dedupe_key", sa.String(512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action_type IN ('send_promised_proposal', 'review_proposal_value')",
            name="ck_review_candidates_action_type",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'resolved', 'dismissed')",
            name="ck_review_candidates_state",
        ),
        sa.CheckConstraint(
            "length(btrim(dedupe_key)) > 0",
            name="ck_review_candidates_dedupe_key_nonblank",
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR state <> 'open'",
            name="ck_review_candidates_resolution_state",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_review_candidates_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_review_candidates_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["evidence.workspace_id", "evidence.id"],
            name="fk_review_candidates_workspace_evidence",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["proposals.workspace_id", "proposals.id"],
            name="fk_review_candidates_workspace_proposal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_candidates"),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_review_candidates_workspace_id"
        ),
    )
    op.create_index(
        "uq_review_candidates_open_dedupe",
        "review_candidates",
        ["workspace_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_candidates_open_dedupe", table_name="review_candidates")
    op.drop_table("review_candidates")
    op.execute(
        "DROP TRIGGER trg_crm_proposal_versions_evidence_tenant ON proposal_versions"
    )
    op.execute("DROP FUNCTION crm_validate_proposal_evidence_tenant()")
    op.drop_constraint(
        "fk_proposal_versions_source_document_evidence_id_evidence",
        "proposal_versions",
        type_="foreignkey",
    )
    op.drop_index("uq_proposals_workspace_thread", table_name="proposals")
    op.drop_constraint(
        "fk_proposals_workspace_sent_evidence", "proposals", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_proposals_workspace_thread_identity", "proposals", type_="foreignkey"
    )
    op.drop_column("proposals", "thread_source_identity_id")
    op.execute("DROP TRIGGER trg_crm_evidence_append_only ON evidence")
    op.execute("DROP FUNCTION crm_reject_evidence_mutation()")
    op.drop_index("ix_evidence_account_captured_at", table_name="evidence")
    op.drop_table("evidence")
