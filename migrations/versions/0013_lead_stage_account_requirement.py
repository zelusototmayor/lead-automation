"""Require accounts for leads in account-required stages.

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa


revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


_ACCOUNT_REQUIRED_STAGES = (
    "'meeting_booked', 'meeting_held', 'proposal_requested', "
    "'proposal_sent', 'negotiation', 'won'"
)
_CONSTRAINT_NAME = "ck_leads_stage_requires_account"


def _has_deployed_constraint() -> bool:
    checks = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_check_constraints("leads")
    }
    return _CONSTRAINT_NAME in checks


def upgrade() -> None:
    adopting_deployed_constraint = _has_deployed_constraint()
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "SELECT 1 FROM leads "
        "WHERE account_id IS NULL "
        f"AND stage IN ({_ACCOUNT_REQUIRED_STAGES})"
        ") THEN "
        "RAISE EXCEPTION "
        "'cannot enforce account-required lead stages while violations exist'; "
        "END IF; END $$"
    )
    if adopting_deployed_constraint:
        op.drop_constraint(_CONSTRAINT_NAME, "leads", type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "leads",
        f"account_id IS NOT NULL OR stage NOT IN ({_ACCOUNT_REQUIRED_STAGES})",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "leads", type_="check")
