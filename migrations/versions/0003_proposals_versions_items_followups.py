"""Create proposals, versions, items, and follow-up history.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


PROPOSAL_STATUSES = (
    "'draft', 'promised', 'sent', 'viewed', 'negotiation', "
    "'won', 'lost', 'withdrawn', 'expired'"
)
SENT_OR_LATER_STATUSES = (
    "'sent', 'viewed', 'negotiation', 'won', 'lost', 'withdrawn', 'expired'"
)


def upgrade() -> None:
    op.create_table(
        "proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("proposal_number", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(32), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sent_verification_state", sa.String(32), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("probability", sa.Numeric(5, 2), nullable=True),
        sa.Column("probability_source", sa.String(64), nullable=True),
        sa.Column("forecast_category", sa.String(64), nullable=True),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("next_action_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("won_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lost_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lost_reason", sa.Text(), nullable=True),
        sa.Column("selected_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "value_state",
            sa.String(32),
            server_default=sa.text("'missing'"),
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
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            f"status IN ({PROPOSAL_STATUSES})", name="ck_proposals_status"
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_proposals_currency_iso"),
        sa.CheckConstraint(
            "probability IS NULL OR probability BETWEEN 0 AND 100",
            name="ck_proposals_probability",
        ),
        sa.CheckConstraint(
            "value_state IN ('missing', 'candidate', 'confirmed', 'rejected')",
            name="ck_proposals_value_state",
        ),
        sa.CheckConstraint(
            "sent_verification_state IS NULL OR "
            "sent_verification_state IN ('verified', 'legacy_unverified')",
            name="ck_proposals_sent_verification_state",
        ),
        sa.CheckConstraint(
            f"(status IN ({SENT_OR_LATER_STATUSES}) AND sent_at IS NOT NULL AND "
            "sent_verification_state IS NOT NULL AND "
            "((sent_verification_state = 'verified' AND sent_evidence_id IS NOT NULL) OR "
            "sent_verification_state = 'legacy_unverified')) "
            f"OR (status NOT IN ({SENT_OR_LATER_STATUSES}) AND sent_at IS NULL AND "
            "sent_evidence_id IS NULL AND sent_verification_state IS NULL)",
            name="ck_proposals_sent_evidence",
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0", name="ck_proposals_title_nonblank"
        ),
        sa.CheckConstraint(
            "proposal_number IS NULL OR length(btrim(proposal_number)) > 0",
            name="ck_proposals_number_nonblank",
        ),
        sa.CheckConstraint(
            "probability_source IS NULL OR length(btrim(probability_source)) > 0",
            name="ck_proposals_probability_source_nonblank",
        ),
        sa.CheckConstraint(
            "forecast_category IS NULL OR length(btrim(forecast_category)) > 0",
            name="ck_proposals_forecast_category_nonblank",
        ),
        sa.CheckConstraint(
            "next_action IS NULL OR length(btrim(next_action)) > 0",
            name="ck_proposals_next_action_nonblank",
        ),
        sa.CheckConstraint(
            "next_action_due_at IS NULL OR next_action IS NOT NULL",
            name="ck_proposals_next_action_due_requires_action",
        ),
        sa.CheckConstraint(
            "lost_reason IS NULL OR length(btrim(lost_reason)) > 0",
            name="ck_proposals_lost_reason_nonblank",
        ),
        sa.CheckConstraint(
            "(status = 'won' AND won_at IS NOT NULL AND lost_at IS NULL AND lost_reason IS NULL) "
            "OR (status = 'lost' AND won_at IS NULL AND lost_at IS NOT NULL AND lost_reason IS NOT NULL) "
            "OR (status NOT IN ('won', 'lost') AND won_at IS NULL AND lost_at IS NULL AND lost_reason IS NULL)",
            name="ck_proposals_close_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_proposals_version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_proposals_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_proposals_workspace_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "lead_id"],
            ["leads.workspace_id", "leads.account_id", "leads.id"],
            name="fk_proposals_workspace_account_lead",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposals"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_proposals_workspace_id"),
    )
    op.create_index("ix_proposals_account_id", "proposals", ["account_id"])
    op.create_index("ix_proposals_lead_id", "proposals", ["lead_id"])

    op.create_table(
        "proposal_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(32), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("one_off_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("mrr_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("arr_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column(
            "tax_inclusion",
            sa.String(16),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column(
            "source_document_evidence_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("extraction_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("confirmed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "version_number > 0", name="ck_proposal_versions_version_number_positive"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'superseded', 'accepted', 'rejected')",
            name="ck_proposal_versions_status",
        ),
        sa.CheckConstraint(
            "tax_inclusion IN ('exclusive', 'inclusive', 'unknown')",
            name="ck_proposal_versions_tax_inclusion",
        ),
        sa.CheckConstraint(
            "one_off_amount IS NULL OR one_off_amount >= 0",
            name="ck_proposal_versions_one_off_nonnegative",
        ),
        sa.CheckConstraint(
            "mrr_amount IS NULL OR mrr_amount >= 0",
            name="ck_proposal_versions_mrr_nonnegative",
        ),
        sa.CheckConstraint(
            "arr_amount IS NULL OR arr_amount >= 0",
            name="ck_proposal_versions_arr_nonnegative",
        ),
        sa.CheckConstraint(
            "extraction_confidence IS NULL OR extraction_confidence BETWEEN 0 AND 1",
            name="ck_proposal_versions_extraction_confidence",
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_proposal_versions_confirmation_pair",
        ),
        sa.CheckConstraint(
            "confirmed_by IS NULL OR source_document_evidence_id IS NOT NULL",
            name="ck_proposal_versions_confirmation_evidence",
        ),
        sa.CheckConstraint(
            "confirmed_by IS NULL OR one_off_amount IS NOT NULL "
            "OR mrr_amount IS NOT NULL OR arr_amount IS NOT NULL",
            name="ck_proposal_versions_confirmation_value",
        ),
        sa.CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL",
            name="ck_proposal_versions_sent_at",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until >= created_at::date",
            name="ck_proposal_versions_valid_interval",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name="fk_proposal_versions_proposal_id_proposals",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposal_versions"),
        sa.UniqueConstraint(
            "proposal_id",
            "version_number",
            name="uq_proposal_versions_proposal_version_number",
        ),
        sa.UniqueConstraint(
            "proposal_id", "id", name="uq_proposal_versions_proposal_id"
        ),
    )
    op.create_index(
        "ix_proposal_versions_proposal_id", "proposal_versions", ["proposal_id"]
    )
    op.create_foreign_key(
        "fk_proposals_selected_version",
        "proposals",
        "proposal_versions",
        ["id", "selected_version_id"],
        ["proposal_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "proposal_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 2), nullable=True),
        sa.Column("billing_period", sa.String(32), nullable=True),
        sa.Column("option_group", sa.Text(), nullable=True),
        sa.Column(
            "is_selected", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.CheckConstraint(
            "length(btrim(description)) > 0",
            name="ck_proposal_items_description_nonblank",
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name="ck_proposal_items_quantity_positive",
        ),
        sa.CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="ck_proposal_items_unit_price_nonnegative",
        ),
        sa.CheckConstraint(
            "amount IS NULL OR amount >= 0", name="ck_proposal_items_amount_nonnegative"
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_proposal_items_currency_iso"
        ),
        sa.CheckConstraint(
            "billing_period IS NULL OR billing_period IN ('mrr', 'arr')",
            name="ck_proposal_items_billing_period",
        ),
        sa.CheckConstraint(
            "option_group IS NULL OR length(btrim(option_group)) > 0",
            name="ck_proposal_items_option_group_nonblank",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_version_id"],
            ["proposal_versions.id"],
            name="fk_proposal_items_proposal_version_id_proposal_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposal_items"),
    )
    op.create_index(
        "ix_proposal_items_proposal_version_id",
        "proposal_items",
        ["proposal_version_id"],
    )
    op.create_index(
        "uq_proposal_items_selected_option",
        "proposal_items",
        ["proposal_version_id", "option_group"],
        unique=True,
        postgresql_where=sa.text("is_selected AND option_group IS NOT NULL"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_validate_proposal_item_currency()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE proposal_currency char(3);
        BEGIN
          IF TG_TABLE_NAME = 'proposals' THEN
            IF EXISTS (
              SELECT 1
                FROM proposal_items pi
                JOIN proposal_versions pv ON pv.id = pi.proposal_version_id
               WHERE pv.proposal_id = NEW.id
                 AND pi.currency IS DISTINCT FROM NEW.currency
            ) THEN
              RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'proposal item currency mismatch';
            END IF;
            RETURN NEW;
          END IF;

          SELECT p.currency INTO proposal_currency
            FROM proposal_versions pv
            JOIN proposals p ON p.id = pv.proposal_id
           WHERE pv.id = NEW.proposal_version_id;
          IF NOT FOUND OR NEW.currency IS DISTINCT FROM proposal_currency THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
              MESSAGE = 'proposal item currency mismatch';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposal_items_validate_currency "
        "AFTER INSERT OR UPDATE ON proposal_items DEFERRABLE INITIALLY IMMEDIATE "
        "FOR EACH ROW EXECUTE FUNCTION crm_validate_proposal_item_currency()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposals_validate_item_currency "
        "AFTER UPDATE OF currency ON proposals DEFERRABLE INITIALLY IMMEDIATE "
        "FOR EACH ROW EXECUTE FUNCTION crm_validate_proposal_item_currency()"
    )

    op.create_table(
        "proposal_followups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "sequence_number > 0", name="ck_proposal_followups_sequence_positive"
        ),
        sa.CheckConstraint(
            "length(btrim(channel)) > 0", name="ck_proposal_followups_channel_nonblank"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name="fk_proposal_followups_proposal_id_proposals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["activity_id"],
            ["activities.id"],
            name="fk_proposal_followups_activity_id_activities",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proposal_followups"),
        sa.UniqueConstraint("activity_id", name="uq_proposal_followups_activity"),
        sa.UniqueConstraint(
            "proposal_id", "sequence_number", name="uq_proposal_followups_sequence"
        ),
    )
    op.create_index(
        "ix_proposal_followups_proposal_id", "proposal_followups", ["proposal_id"]
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_enforce_proposal_version_append_only()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.status <> 'superseded'
               AND NEW.status = 'superseded'
               AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'proposal version history is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_proposal_versions_append_only
        BEFORE UPDATE OR DELETE ON proposal_versions
        FOR EACH ROW EXECUTE FUNCTION crm_enforce_proposal_version_append_only()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_lock_proposal_aggregate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target_proposal_id uuid;
        BEGIN
          IF TG_TABLE_NAME = 'proposals' THEN
            target_proposal_id := NEW.id;
          ELSIF TG_TABLE_NAME IN ('proposal_versions', 'proposal_followups') THEN
            target_proposal_id := NEW.proposal_id;
          ELSIF TG_TABLE_NAME = 'proposal_items' THEN
            SELECT proposal_id INTO target_proposal_id
              FROM proposal_versions
             WHERE id = NEW.proposal_version_id;
          END IF;
          IF target_proposal_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23503',
              MESSAGE = 'proposal aggregate context missing';
          END IF;
          PERFORM pg_advisory_xact_lock(
            hashtextextended('crm-proposal:' || target_proposal_id::text, 0)
          );
          RETURN NEW;
        END; $$
        """
    )
    for table_name in (
        "proposals",
        "proposal_versions",
        "proposal_items",
        "proposal_followups",
    ):
        op.execute(
            f"CREATE TRIGGER trg_crm_{table_name}_lock_aggregate "
            f"BEFORE INSERT OR UPDATE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION crm_lock_proposal_aggregate()"
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_validate_proposal_lead_context()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE referenced_account_id uuid;
        BEGIN
          IF NEW.lead_id IS NOT NULL THEN
            SELECT account_id INTO referenced_account_id
              FROM leads
              WHERE workspace_id = NEW.workspace_id AND id = NEW.lead_id;
            IF NOT FOUND OR referenced_account_id IS DISTINCT FROM NEW.account_id THEN
              RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'proposal lead context mismatch';
            END IF;
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposals_validate_lead_context "
        "AFTER INSERT OR UPDATE ON proposals "
        "DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW "
        "EXECUTE FUNCTION crm_validate_proposal_lead_context()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_validate_confirmed_proposal_value()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_TABLE_NAME = 'proposals' THEN
            IF NEW.value_state = 'confirmed' AND NOT EXISTS (
              SELECT 1
                FROM proposal_versions pv
               WHERE pv.id = NEW.selected_version_id
                 AND pv.proposal_id = NEW.id
                 AND pv.status NOT IN ('rejected', 'superseded')
                 AND pv.source_document_evidence_id IS NOT NULL
                 AND pv.confirmed_by IS NOT NULL
                 AND pv.confirmed_at IS NOT NULL
                 AND (pv.one_off_amount IS NOT NULL
                      OR pv.mrr_amount IS NOT NULL
                      OR pv.arr_amount IS NOT NULL)
            ) THEN
              RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'confirmed proposal value requires evidence';
            END IF;
            RETURN NEW;
          END IF;

          IF EXISTS (
            SELECT 1
              FROM proposals p
             WHERE p.selected_version_id = NEW.id
               AND p.value_state = 'confirmed'
               AND (NEW.status IN ('rejected', 'superseded')
                    OR NEW.source_document_evidence_id IS NULL
                    OR NEW.confirmed_by IS NULL
                    OR NEW.confirmed_at IS NULL
                    OR (NEW.one_off_amount IS NULL
                        AND NEW.mrr_amount IS NULL
                        AND NEW.arr_amount IS NULL))
          ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
              MESSAGE = 'confirmed proposal value requires evidence';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposals_validate_confirmed_value "
        "AFTER INSERT OR UPDATE OF value_state, selected_version_id ON proposals "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION crm_validate_confirmed_proposal_value()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposal_versions_validate_confirmed_value "
        "AFTER UPDATE ON proposal_versions DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION crm_validate_confirmed_proposal_value()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION crm_validate_proposal_followup_context()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE proposal_workspace_id uuid;
        DECLARE proposal_account_id uuid;
        DECLARE activity_workspace_id uuid;
        DECLARE activity_account_id uuid;
        BEGIN
          IF TG_TABLE_NAME = 'proposals' THEN
            IF EXISTS (
              SELECT 1
                FROM proposal_followups pf
                JOIN activities a ON a.id = pf.activity_id
               WHERE pf.proposal_id = NEW.id
                 AND (a.workspace_id IS DISTINCT FROM NEW.workspace_id
                      OR a.account_id IS DISTINCT FROM NEW.account_id)
            ) THEN
              RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'proposal follow-up context mismatch';
            END IF;
            RETURN NEW;
          END IF;

          SELECT workspace_id, account_id
            INTO proposal_workspace_id, proposal_account_id
            FROM proposals WHERE id = NEW.proposal_id;
          SELECT workspace_id, account_id
            INTO activity_workspace_id, activity_account_id
            FROM activities WHERE id = NEW.activity_id;
          IF proposal_workspace_id IS DISTINCT FROM activity_workspace_id
             OR proposal_account_id IS DISTINCT FROM activity_account_id THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
              MESSAGE = 'proposal follow-up context mismatch';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposal_followups_validate_context "
        "AFTER INSERT OR UPDATE ON proposal_followups DEFERRABLE INITIALLY IMMEDIATE "
        "FOR EACH ROW EXECUTE FUNCTION crm_validate_proposal_followup_context()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_crm_proposals_validate_followup_context "
        "AFTER UPDATE ON proposals DEFERRABLE INITIALLY IMMEDIATE "
        "FOR EACH ROW EXECUTE FUNCTION crm_validate_proposal_followup_context()"
    )


def downgrade() -> None:
    for table_name in (
        "proposal_followups",
        "proposal_items",
        "proposal_versions",
        "proposals",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_crm_{table_name}_lock_aggregate "
            f"ON {table_name}"
        )
    op.execute("DROP FUNCTION IF EXISTS crm_lock_proposal_aggregate()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_proposals_validate_item_currency ON proposals"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_proposal_items_validate_currency ON proposal_items"
    )
    op.execute("DROP FUNCTION IF EXISTS crm_validate_proposal_item_currency()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_proposal_versions_append_only ON proposal_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS crm_enforce_proposal_version_append_only()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_proposals_validate_followup_context ON proposals"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_proposal_followups_validate_context ON proposal_followups"
    )
    op.execute("DROP FUNCTION IF EXISTS crm_validate_proposal_followup_context()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_proposal_versions_validate_confirmed_value "
        "ON proposal_versions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_proposals_validate_confirmed_value ON proposals"
    )
    op.execute("DROP FUNCTION IF EXISTS crm_validate_confirmed_proposal_value()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_crm_proposals_validate_lead_context ON proposals"
    )
    op.execute("DROP FUNCTION IF EXISTS crm_validate_proposal_lead_context()")
    op.drop_index("ix_proposal_followups_proposal_id", table_name="proposal_followups")
    op.drop_table("proposal_followups")
    op.drop_index("uq_proposal_items_selected_option", table_name="proposal_items")
    op.drop_index("ix_proposal_items_proposal_version_id", table_name="proposal_items")
    op.drop_table("proposal_items")
    op.drop_constraint("fk_proposals_selected_version", "proposals", type_="foreignkey")
    op.drop_index("ix_proposal_versions_proposal_id", table_name="proposal_versions")
    op.drop_table("proposal_versions")
    op.drop_index("ix_proposals_lead_id", table_name="proposals")
    op.drop_index("ix_proposals_account_id", table_name="proposals")
    op.drop_table("proposals")
