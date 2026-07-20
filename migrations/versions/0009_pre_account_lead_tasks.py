"""Allow operational lead tasks before an account exists.

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column("tasks", "account_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        "ck_tasks_requires_account_or_lead",
        "tasks",
        "account_id IS NOT NULL OR lead_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_tasks_proposal_requires_account",
        "tasks",
        "proposal_id IS NULL OR account_id IS NOT NULL",
    )
    op.create_foreign_key(
        "fk_tasks_workspace_lead",
        "tasks",
        "leads",
        ["workspace_id", "lead_id"],
        ["workspace_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tasks_workspace_completion_activity",
        "tasks",
        "activities",
        ["workspace_id", "completion_activity_id"],
        ["workspace_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tasks_workspace_completion_activity", "tasks", type_="foreignkey"
    )
    op.drop_constraint("fk_tasks_workspace_lead", "tasks", type_="foreignkey")
    op.drop_constraint("ck_tasks_proposal_requires_account", "tasks", type_="check")
    op.drop_constraint("ck_tasks_requires_account_or_lead", "tasks", type_="check")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM tasks WHERE account_id IS NULL) THEN "
        "RAISE EXCEPTION 'cannot downgrade while pre-account tasks exist'; "
        "END IF; END $$"
    )
    op.alter_column("tasks", "account_id", existing_type=sa.UUID(), nullable=False)
