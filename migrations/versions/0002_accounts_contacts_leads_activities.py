"""Create relational CRM aggregates and immutable activities.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

SOURCE_SYSTEMS = (
    "'google_sheets', 'gmail', 'google_calendar', 'granola', 'manual', 'agent'"
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_source_identities_workspace_id", "source_identities", ["workspace_id", "id"]
    )
    op.create_unique_constraint(
        "uq_ingest_events_workspace_id", "ingest_events", ["workspace_id", "id"]
    )
    op.add_column(
        "ingest_events",
        sa.Column("stage_reduction_fingerprint", sa.String(64), nullable=True),
    )
    op.create_check_constraint(
        "ck_ingest_events_stage_reduction_fingerprint",
        "ingest_events",
        "stage_reduction_fingerprint IS NULL OR "
        "stage_reduction_fingerprint ~ '^[0-9a-f]{64}$'",
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("legal_name", sa.String(512), nullable=True),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("normalized_name", sa.String(512), nullable=False),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column("primary_domain", postgresql.CITEXT(), nullable=True),
        sa.Column(
            "lifecycle_stage",
            sa.String(32),
            server_default=sa.text("'potential'"),
            nullable=False,
        ),
        sa.Column(
            "highest_stage_rank",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "merged_into_account_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("sector", sa.String(255), nullable=True),
        sa.Column("commercial_vertical", sa.String(255), nullable=True),
        sa.Column("source_origin", sa.String(255), nullable=True),
        sa.Column("source_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            "length(btrim(display_name)) > 0", name="ck_accounts_display_name_nonblank"
        ),
        sa.CheckConstraint(
            "length(btrim(normalized_name)) > 0",
            name="ck_accounts_normalized_name_nonblank",
        ),
        sa.CheckConstraint(
            "legal_name IS NULL OR length(btrim(legal_name)) > 0",
            name="ck_accounts_legal_name_nonblank",
        ),
        sa.CheckConstraint(
            "website_url IS NULL OR length(btrim(website_url)) > 0",
            name="ck_accounts_website_url_nonblank",
        ),
        sa.CheckConstraint(
            "primary_domain IS NULL OR length(btrim(primary_domain::text)) > 0",
            name="ck_accounts_primary_domain_nonblank",
        ),
        sa.CheckConstraint(
            "sector IS NULL OR length(btrim(sector)) > 0",
            name="ck_accounts_sector_nonblank",
        ),
        sa.CheckConstraint(
            "commercial_vertical IS NULL OR length(btrim(commercial_vertical)) > 0",
            name="ck_accounts_vertical_nonblank",
        ),
        sa.CheckConstraint(
            "source_origin IS NULL OR length(btrim(source_origin)) > 0",
            name="ck_accounts_source_origin_nonblank",
        ),
        sa.CheckConstraint(
            "merged_into_account_id IS NULL OR merged_into_account_id <> id",
            name="ck_accounts_not_self_merged",
        ),
        sa.CheckConstraint(
            "lifecycle_stage IN ('potential', 'meeting', 'proposal', 'customer', 'lost', 'inactive')",
            name="ck_accounts_lifecycle_stage",
        ),
        sa.CheckConstraint(
            "highest_stage_rank BETWEEN 0 AND 90", name="ck_accounts_highest_stage_rank"
        ),
        sa.CheckConstraint(
            "source_confidence IS NULL OR source_confidence BETWEEN 0 AND 1",
            name="ck_accounts_source_confidence",
        ),
        sa.CheckConstraint("version > 0", name="ck_accounts_version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_accounts_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "merged_into_account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_accounts_workspace_merged_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_accounts_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_accounts_workspace_id"),
    )
    op.create_index(
        "ix_accounts_workspace_normalized_name",
        "accounts",
        ["workspace_id", "normalized_name"],
    )

    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("full_name", sa.String(512), nullable=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("primary_email", postgresql.CITEXT(), nullable=True),
        sa.Column(
            "is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "status", sa.String(16), server_default=sa.text("'active'"), nullable=False
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
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name="ck_contacts_status"
        ),
        sa.CheckConstraint(
            "primary_email IS NULL OR length(btrim(primary_email::text)) > 0",
            name="ck_contacts_primary_email_nonblank",
        ),
        sa.CheckConstraint(
            "full_name IS NULL OR length(btrim(full_name)) > 0",
            name="ck_contacts_full_name_nonblank",
        ),
        sa.CheckConstraint(
            "title IS NULL OR length(btrim(title)) > 0",
            name="ck_contacts_title_nonblank",
        ),
        sa.CheckConstraint(
            "phone IS NULL OR length(btrim(phone)) > 0",
            name="ck_contacts_phone_nonblank",
        ),
        sa.CheckConstraint("version > 0", name="ck_contacts_version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_contacts_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_contacts_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_contacts"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_contacts_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_contacts_workspace_account_id"
        ),
    )
    op.create_index("ix_contacts_account_id", "contacts", ["account_id"])
    op.create_index(
        "uq_contacts_workspace_primary_email",
        "contacts",
        ["workspace_id", "primary_email"],
        unique=True,
        postgresql_where=sa.text("primary_email IS NOT NULL"),
    )

    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_stage_raw", sa.String(255), nullable=True),
        sa.Column(
            "stage", sa.String(32), server_default=sa.text("'new'"), nullable=False
        ),
        sa.Column(
            "highest_stage_rank",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("priority", sa.String(64), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sector", sa.String(255), nullable=True),
        sa.Column("commercial_vertical", sa.String(255), nullable=True),
        sa.Column("source_origin", sa.String(255), nullable=True),
        sa.Column("source_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            "stage IN ('new', 'contacted', 'qualified', 'meeting_booked', 'meeting_held', 'proposal_requested', 'proposal_sent', 'negotiation', 'won', 'lost', 'not_a_fit')",
            name="ck_leads_stage",
        ),
        sa.CheckConstraint(
            "contact_id IS NULL OR account_id IS NOT NULL",
            name="ck_leads_contact_requires_account",
        ),
        sa.CheckConstraint(
            "source_stage_raw IS NULL OR length(btrim(source_stage_raw)) > 0",
            name="ck_leads_source_stage_raw_nonblank",
        ),
        sa.CheckConstraint(
            "priority IS NULL OR length(btrim(priority)) > 0",
            name="ck_leads_priority_nonblank",
        ),
        sa.CheckConstraint(
            "sector IS NULL OR length(btrim(sector)) > 0",
            name="ck_leads_sector_nonblank",
        ),
        sa.CheckConstraint(
            "commercial_vertical IS NULL OR length(btrim(commercial_vertical)) > 0",
            name="ck_leads_vertical_nonblank",
        ),
        sa.CheckConstraint(
            "source_origin IS NULL OR length(btrim(source_origin)) > 0",
            name="ck_leads_source_origin_nonblank",
        ),
        sa.CheckConstraint(
            "highest_stage_rank BETWEEN 0 AND 90", name="ck_leads_highest_stage_rank"
        ),
        sa.CheckConstraint("version > 0", name="ck_leads_version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_leads_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_leads_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_leads_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "contact_id"],
            ["contacts.workspace_id", "contacts.account_id", "contacts.id"],
            name="fk_leads_workspace_account_contact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_leads_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_leads_workspace_account_id"
        ),
    )
    op.create_index("ix_leads_account_id", "leads", ["account_id"])
    op.create_index("ix_leads_contact_id", "leads", ["contact_id"])

    op.create_table(
        "activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("activity_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("semantic_fingerprint", sa.String(64), nullable=True),
        sa.Column("direction", sa.String(32), nullable=True),
        sa.Column("source_system", sa.String(32), nullable=True),
        sa.Column("source_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ingest_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(64), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "supersedes_activity_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "activity_type IN ('stage_change', 'call', 'email_sent', 'email_received', 'meeting', 'proposal', 'note', 'task', 'system')",
            name="ck_activities_activity_type",
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0", name="ck_activities_title_nonblank"
        ),
        sa.CheckConstraint(
            "summary IS NULL OR length(btrim(summary)) > 0",
            name="ck_activities_summary_nonblank",
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('inbound', 'outbound', 'internal')",
            name="ck_activities_direction",
        ),
        sa.CheckConstraint(
            f"source_system IS NULL OR source_system IN ({SOURCE_SYSTEMS})",
            name="ck_activities_source_system",
        ),
        sa.CheckConstraint(
            "actor_type IS NULL OR length(btrim(actor_type)) > 0",
            name="ck_activities_actor_type_nonblank",
        ),
        sa.CheckConstraint(
            "supersedes_activity_id IS NULL OR supersedes_activity_id <> id",
            name="ck_activities_not_self_superseding",
        ),
        sa.CheckConstraint(
            "account_id IS NOT NULL OR lead_id IS NOT NULL",
            name="ck_activities_requires_entity",
        ),
        sa.CheckConstraint(
            "contact_id IS NULL OR account_id IS NOT NULL",
            name="ck_activities_contact_requires_account",
        ),
        sa.CheckConstraint(
            "(semantic_fingerprint IS NULL OR semantic_fingerprint ~ '^[0-9a-f]{64}$') "
            "AND (activity_type <> 'stage_change' OR semantic_fingerprint IS NOT NULL)",
            name="ck_activities_semantic_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_activities_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_activities_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "lead_id"],
            ["leads.workspace_id", "leads.id"],
            name="fk_activities_workspace_lead",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "lead_id"],
            ["leads.workspace_id", "leads.account_id", "leads.id"],
            name="fk_activities_workspace_account_lead",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "contact_id"],
            ["contacts.workspace_id", "contacts.account_id", "contacts.id"],
            name="fk_activities_workspace_account_contact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_identity_id"],
            ["source_identities.workspace_id", "source_identities.id"],
            name="fk_activities_workspace_source_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "ingest_event_id"],
            ["ingest_events.workspace_id", "ingest_events.id"],
            name="fk_activities_workspace_ingest_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "supersedes_activity_id"],
            ["activities.workspace_id", "activities.id"],
            name="fk_activities_workspace_supersedes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "supersedes_activity_id"],
            ["activities.workspace_id", "activities.account_id", "activities.id"],
            name="fk_activities_workspace_account_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_activities"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_activities_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_activities_workspace_account_id",
        ),
    )
    op.create_index(
        "ix_activities_account_occurred_at", "activities", ["account_id", "occurred_at"]
    )
    op.create_index("ix_activities_lead_id", "activities", ["lead_id"])
    op.create_index(
        "uq_activities_workspace_ingest_type",
        "activities",
        ["workspace_id", "ingest_event_id", "activity_type"],
        unique=True,
        postgresql_where=sa.text("ingest_event_id IS NOT NULL"),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_reject_activity_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'activities are immutable';
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_crm_activities_immutable BEFORE UPDATE OR DELETE ON activities "
        "FOR EACH ROW EXECUTE FUNCTION crm_reject_activity_mutation()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_validate_activity_context()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE referenced_account_id uuid;
        BEGIN
          IF NEW.lead_id IS NOT NULL THEN
            SELECT account_id INTO referenced_account_id
              FROM leads
              WHERE workspace_id = NEW.workspace_id AND id = NEW.lead_id;
            IF NOT FOUND OR referenced_account_id IS DISTINCT FROM NEW.account_id THEN
              RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'activity context mismatch';
            END IF;
          END IF;
          IF NEW.supersedes_activity_id IS NOT NULL THEN
            SELECT account_id INTO referenced_account_id
              FROM activities
              WHERE workspace_id = NEW.workspace_id
                AND id = NEW.supersedes_activity_id;
            IF NOT FOUND OR referenced_account_id IS DISTINCT FROM NEW.account_id THEN
              RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'activity context mismatch';
            END IF;
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_crm_activities_validate_context "
        "BEFORE INSERT ON activities FOR EACH ROW "
        "EXECUTE FUNCTION crm_validate_activity_context()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_activities_validate_context ON activities"
    )
    op.execute("DROP FUNCTION IF EXISTS crm_validate_activity_context()")
    op.execute("DROP TRIGGER IF EXISTS trg_crm_activities_immutable ON activities")
    op.execute("DROP FUNCTION IF EXISTS crm_reject_activity_mutation()")
    op.drop_index("uq_activities_workspace_ingest_type", table_name="activities")
    op.drop_index("ix_activities_lead_id", table_name="activities")
    op.drop_index("ix_activities_account_occurred_at", table_name="activities")
    op.drop_table("activities")
    op.drop_index("ix_leads_contact_id", table_name="leads")
    op.drop_index("ix_leads_account_id", table_name="leads")
    op.drop_table("leads")
    op.drop_index("uq_contacts_workspace_primary_email", table_name="contacts")
    op.drop_index("ix_contacts_account_id", table_name="contacts")
    op.drop_table("contacts")
    op.drop_index("ix_accounts_workspace_normalized_name", table_name="accounts")
    op.drop_table("accounts")
    op.drop_constraint(
        "ck_ingest_events_stage_reduction_fingerprint",
        "ingest_events",
        type_="check",
    )
    op.drop_column("ingest_events", "stage_reduction_fingerprint")
    op.execute(
        "ALTER TABLE ingest_events DROP CONSTRAINT IF EXISTS uq_ingest_events_workspace_id"
    )
    op.execute(
        "ALTER TABLE source_identities DROP CONSTRAINT IF EXISTS uq_source_identities_workspace_id"
    )
