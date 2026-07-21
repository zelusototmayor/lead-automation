"""Add structured stage transition facts to activities.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


_STAGE_VALUES = (
    "'new', 'contacted', 'qualified', 'meeting_booked', 'meeting_held', "
    "'proposal_requested', 'proposal_sent', 'negotiation', 'won', 'lost', "
    "'not_a_fit'"
)


def upgrade() -> None:
    op.add_column("activities", sa.Column("from_stage", sa.String(32), nullable=True))
    op.add_column("activities", sa.Column("to_stage", sa.String(32), nullable=True))
    op.create_check_constraint(
        "ck_activities_stage_transition_pair",
        "activities",
        "(from_stage IS NULL AND to_stage IS NULL) OR "
        "(from_stage IS NOT NULL AND to_stage IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_activities_stage_transition_type",
        "activities",
        "activity_type = 'stage_change' OR (from_stage IS NULL AND to_stage IS NULL)",
    )
    op.create_check_constraint(
        "ck_activities_stage_transition_values",
        "activities",
        "(from_stage IS NULL AND to_stage IS NULL) OR "
        f"(from_stage IN ({_STAGE_VALUES}) AND to_stage IN ({_STAGE_VALUES}) "
        "AND from_stage <> to_stage)",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "SELECT 1 FROM activities "
        "WHERE from_stage IS NOT NULL OR to_stage IS NOT NULL"
        ") THEN "
        "RAISE EXCEPTION "
        "'cannot downgrade while structured stage transitions exist'; "
        "END IF; END $$"
    )
    op.drop_constraint(
        "ck_activities_stage_transition_values", "activities", type_="check"
    )
    op.drop_constraint(
        "ck_activities_stage_transition_type", "activities", type_="check"
    )
    op.drop_constraint(
        "ck_activities_stage_transition_pair", "activities", type_="check"
    )
    op.drop_column("activities", "to_stage")
    op.drop_column("activities", "from_stage")
