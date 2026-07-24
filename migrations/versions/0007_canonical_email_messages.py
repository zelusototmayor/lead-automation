"""Add canonical engagements and reconciliation runs.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_source_identities_entity_kind", "source_identities", type_="check"
    )
    op.create_check_constraint(
        "ck_source_identities_entity_kind",
        "source_identities",
        "entity_kind IN ('lead', 'account', 'contact', 'message', 'mailbox', "
        "'thread', 'meeting', 'proposal', 'document')",
    )
    op.create_unique_constraint(
        "uq_source_identities_workspace_id_semantics",
        "source_identities",
        ["workspace_id", "id", "source_system", "entity_kind"],
    )
    op.create_unique_constraint(
        "uq_evidence_workspace_account_id_type",
        "evidence",
        ["workspace_id", "account_id", "id", "evidence_type"],
    )
    op.create_table(
        "email_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mailbox_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "mailbox_source_system",
            sa.String(32),
            server_default=sa.text("'gmail'"),
            nullable=False,
        ),
        sa.Column(
            "mailbox_entity_kind",
            sa.String(32),
            server_default=sa.text("'mailbox'"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(512), nullable=False),
        sa.Column("provider_thread_id", sa.String(512), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_address", postgresql.CITEXT(), nullable=True),
        sa.Column(
            "to_addresses",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_preview_redacted", sa.Text(), nullable=True),
        sa.Column(
            "has_attachments",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("proposal_candidate_state", sa.String(32), nullable=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "evidence_type",
            sa.String(32),
            server_default=sa.text("'email_message'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_email_messages_direction"
        ),
        sa.CheckConstraint(
            "length(btrim(provider_message_id)) > 0",
            name="ck_email_messages_provider_message_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider_thread_id)) > 0",
            name="ck_email_messages_provider_thread_id_nonblank",
        ),
        sa.CheckConstraint(
            "mailbox_source_system = 'gmail' AND mailbox_entity_kind = 'mailbox'",
            name="ck_email_messages_mailbox_identity",
        ),
        sa.CheckConstraint(
            "from_address IS NULL OR octet_length(from_address::text) <= 320",
            name="ck_email_messages_from_address_bounded",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(to_addresses) = 'array' "
            "AND octet_length(to_addresses::text) <= 4096 "
            "AND NOT jsonb_path_exists(to_addresses, '$[*] ? (@.type() != \"string\")') "
            "AND NOT jsonb_path_exists(to_addresses, "
            '\'$[*] ? (@ like_regex "^\\\\s*$" || @ like_regex "^.{254}.+$" flag "s")\')',
            name="ck_email_messages_to_addresses_minimized",
        ),
        sa.CheckConstraint(
            "evidence_type = 'email_message'",
            name="ck_email_messages_evidence_type",
        ),
        sa.CheckConstraint(
            "subject IS NULL OR octet_length(subject) <= 512",
            name="ck_email_messages_subject_bounded",
        ),
        sa.CheckConstraint(
            "body_preview_redacted IS NULL OR octet_length(body_preview_redacted) <= 2048",
            name="ck_email_messages_body_preview_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_email_messages_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "contact_id"],
            ["contacts.workspace_id", "contacts.account_id", "contacts.id"],
            name="fk_email_messages_workspace_account_contact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id", name="pk_email_messages"),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_email_messages_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "mailbox_identity_id",
            "provider_message_id",
            name="uq_email_messages_workspace_mailbox_provider",
        ),
    )
    op.create_index(
        "ix_email_messages_account_sent_at",
        "email_messages",
        ["account_id", "sent_at"],
    )
    op.create_table(
        "meetings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("calendar_id", sa.String(512), nullable=False),
        sa.Column("external_event_id", sa.String(512), nullable=False),
        sa.Column("occurrence_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("needs", sa.Text(), nullable=True),
        sa.Column("objections", sa.Text(), nullable=True),
        sa.Column("decisions", sa.Text(), nullable=True),
        sa.Column("commitments", sa.Text(), nullable=True),
        sa.Column(
            "next_steps",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("notes_evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "notes_evidence_type",
            sa.String(32),
            server_default=sa.text("'meeting_note'"),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "status IN ('booked', 'held', 'cancelled', 'no_show')",
            name="ck_meetings_status",
        ),
        sa.CheckConstraint(
            "(status = 'held' AND held_at IS NOT NULL) OR "
            "(status <> 'held' AND held_at IS NULL)",
            name="ck_meetings_held_state",
        ),
        sa.CheckConstraint(
            "scheduled_end_at IS NULL OR scheduled_end_at >= scheduled_start_at",
            name="ck_meetings_schedule_order",
        ),
        sa.CheckConstraint(
            "(summary IS NULL OR octet_length(summary) <= 4096) "
            "AND (needs IS NULL OR octet_length(needs) <= 4096) "
            "AND (objections IS NULL OR octet_length(objections) <= 4096) "
            "AND (decisions IS NULL OR octet_length(decisions) <= 4096) "
            "AND (commitments IS NULL OR octet_length(commitments) <= 4096)",
            name="ck_meetings_text_minimized",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(next_steps) = 'object' "
            "AND octet_length(next_steps::text) <= 4096 "
            "AND (next_steps - ARRAY['action', 'owner', 'due_at', 'status']) = '{}'::jsonb "
            'AND NOT jsonb_path_exists(next_steps, \'$.* ? (@.type() != "string" && @.type() != "null")\')',
            name="ck_meetings_next_steps_minimized",
        ),
        sa.CheckConstraint(
            "notes_evidence_type = 'meeting_note'",
            name="ck_meetings_notes_evidence_type",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_meetings_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "lead_id"],
            ["leads.workspace_id", "leads.id"],
            name="fk_meetings_workspace_lead",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "lead_id"],
            ["leads.workspace_id", "leads.account_id", "leads.id"],
            name="fk_meetings_workspace_account_lead",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id", name="pk_meetings"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_meetings_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "calendar_id",
            "external_event_id",
            "occurrence_start_at",
            name="uq_meetings_workspace_provider_occurrence",
        ),
    )
    op.create_index(
        "ix_meetings_account_scheduled",
        "meetings",
        ["account_id", "scheduled_start_at"],
    )
    op.create_unique_constraint(
        "uq_proposals_workspace_account_id",
        "proposals",
        ["workspace_id", "account_id", "id"],
    )
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", sa.String(16), server_default=sa.text("'open'"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "completion_activity_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("source_rule", sa.String(128), nullable=True),
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
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "length(btrim(task_type)) > 0", name="ck_tasks_task_type_nonblank"
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_tasks_title_nonblank"),
        sa.CheckConstraint("octet_length(title) <= 512", name="ck_tasks_title_bounded"),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')", name="ck_tasks_status"
        ),
        sa.CheckConstraint(
            "((status = 'completed' AND completed_at IS NOT NULL "
            "AND completion_activity_id IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL "
            "AND completion_activity_id IS NULL)) IS TRUE",
            name="ck_tasks_completion_state",
        ),
        sa.CheckConstraint(
            "source_rule IS NULL OR length(btrim(source_rule)) > 0",
            name="ck_tasks_source_rule_nonblank",
        ),
        sa.CheckConstraint("version > 0", name="ck_tasks_version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_tasks_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "proposal_id"],
            ["proposals.workspace_id", "proposals.account_id", "proposals.id"],
            name="fk_tasks_workspace_account_proposal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "completion_activity_id"],
            ["activities.workspace_id", "activities.account_id", "activities.id"],
            name="fk_tasks_workspace_account_completion_activity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tasks_workspace_id"),
    )
    op.create_index(
        "ix_tasks_account_status_due", "tasks", ["account_id", "status", "due_at"]
    )
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column("source_scope", sa.String(255), nullable=False),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "scanned_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "updated_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "duplicate_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "conflict_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "error_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "report",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(connector)) > 0",
            name="ck_reconciliation_runs_connector_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(source_scope)) > 0",
            name="ck_reconciliation_runs_source_scope_nonblank",
        ),
        sa.CheckConstraint(
            "window_end_at >= window_start_at",
            name="ck_reconciliation_runs_window_order",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_reconciliation_runs_finish_order",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_reconciliation_runs_status",
        ),
        sa.CheckConstraint(
            "((status = 'running' AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)) IS TRUE",
            name="ck_reconciliation_runs_finish_state",
        ),
        sa.CheckConstraint(
            "scanned_count >= 0 AND created_count >= 0 AND updated_count >= 0 "
            "AND duplicate_count >= 0 AND conflict_count >= 0 AND error_count >= 0",
            name="ck_reconciliation_runs_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(report) = 'object' AND octet_length(report::text) <= 512 "
            "AND (report - ARRAY['duplicate', 'conflict', 'error', 'unmapped', 'missing']) = '{}'::jsonb "
            "AND NOT jsonb_path_exists(report, '$.* ? (@.type() != \"number\" || @ < 0)')",
            name="ck_reconciliation_runs_report_minimized",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reconciliation_runs"),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_reconciliation_runs_workspace_id"
        ),
    )
    op.create_index(
        "ix_reconciliation_runs_workspace_connector_started",
        "reconciliation_runs",
        ["workspace_id", "connector", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reconciliation_runs_workspace_connector_started",
        table_name="reconciliation_runs",
    )
    op.drop_table("reconciliation_runs")
    op.drop_index("ix_tasks_account_status_due", table_name="tasks")
    op.drop_table("tasks")
    op.drop_constraint("uq_proposals_workspace_account_id", "proposals", type_="unique")
    op.drop_index("ix_meetings_account_scheduled", table_name="meetings")
    op.drop_table("meetings")
    op.drop_index("ix_email_messages_account_sent_at", table_name="email_messages")
    op.drop_table("email_messages")
    op.drop_constraint(
        "uq_evidence_workspace_account_id_type", "evidence", type_="unique"
    )
    op.drop_constraint(
        "uq_source_identities_workspace_id_semantics",
        "source_identities",
        type_="unique",
    )
    op.drop_constraint(
        "ck_source_identities_entity_kind", "source_identities", type_="check"
    )
    op.execute("DELETE FROM source_identities WHERE entity_kind = 'mailbox'")
    op.create_check_constraint(
        "ck_source_identities_entity_kind",
        "source_identities",
        "entity_kind IN ('lead', 'account', 'contact', 'message', 'thread', "
        "'meeting', 'proposal', 'document')",
    )
