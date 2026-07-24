"""Add structured stage transition facts to activities.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa


revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


_STAGE_VALUES = (
    "'new', 'contacted', 'qualified', 'meeting_booked', 'meeting_held', "
    "'proposal_requested', 'proposal_sent', 'negotiation', 'won', 'lost', "
    "'not_a_fit'"
)


_STAGE_COLUMN_NAMES = {"from_stage", "to_stage"}
_STAGE_CONSTRAINT_NAMES = {
    "ck_activities_stage_transition_pair",
    "ck_activities_stage_transition_type",
    "ck_activities_stage_transition_values",
}


def _has_compatible_deployed_stage_schema() -> bool:
    """Adopt the previously deployed parity schema after lineage repair."""
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns("activities")}
    checks = {
        item["name"]: item.get("sqltext", "")
        for item in inspector.get_check_constraints("activities")
    }
    present_columns = _STAGE_COLUMN_NAMES & columns.keys()
    present_checks = _STAGE_CONSTRAINT_NAMES & checks.keys()
    if not present_columns and not present_checks:
        return False
    if (
        present_columns != _STAGE_COLUMN_NAMES
        or present_checks != _STAGE_CONSTRAINT_NAMES
    ):
        raise RuntimeError("incompatible deployed stage-transition schema")
    for name in _STAGE_COLUMN_NAMES:
        column = columns[name]
        if not column["nullable"] or not isinstance(column["type"], sa.String):
            raise RuntimeError("incompatible deployed stage-transition schema")
        if column["type"].length != 32:
            raise RuntimeError("incompatible deployed stage-transition schema")
    return True


def upgrade() -> None:
    adopting_deployed_schema = _has_compatible_deployed_stage_schema()
    if not adopting_deployed_schema:
        op.add_column(
            "activities", sa.Column("from_stage", sa.String(32), nullable=True)
        )
        op.add_column("activities", sa.Column("to_stage", sa.String(32), nullable=True))
    else:
        for name in sorted(_STAGE_CONSTRAINT_NAMES, reverse=True):
            op.drop_constraint(name, "activities", type_="check")
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
