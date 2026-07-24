"""Add lead-specific tasks and structured call outcomes.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tasks", sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("activities", sa.Column("outcome_code", sa.String(32), nullable=True))
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1
              FROM tasks AS task
             WHERE task.task_type IN ('call', 'email')
               AND (
                 SELECT count(*)
                   FROM leads AS lead
                  WHERE lead.workspace_id = task.workspace_id
                    AND lead.account_id = task.account_id
               ) <> 1
          ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
              MESSAGE = 'cannot assign lead context to existing operational tasks';
          END IF;
        END $$
        """
    )
    op.execute(
        """
        UPDATE tasks AS task
           SET lead_id = (
             SELECT lead.id
               FROM leads AS lead
              WHERE lead.workspace_id = task.workspace_id
                AND lead.account_id = task.account_id
           )
         WHERE task.task_type IN ('call', 'email')
        """
    )
    op.create_check_constraint(
        "ck_activities_outcome_code_nonblank",
        "activities",
        "outcome_code IS NULL OR length(btrim(outcome_code)) > 0",
    )
    op.create_check_constraint(
        "ck_tasks_lead_context",
        "tasks",
        "(task_type NOT IN ('call', 'email') OR lead_id IS NOT NULL) IS TRUE",
    )
    op.create_foreign_key(
        "fk_tasks_workspace_account_lead",
        "tasks",
        "leads",
        ["workspace_id", "account_id", "lead_id"],
        ["workspace_id", "account_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_tasks_workspace_lead_status_due",
        "tasks",
        ["workspace_id", "lead_id", "status", "due_at"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM activities WHERE outcome_code IS NOT NULL
          ) OR EXISTS (
            SELECT 1
              FROM tasks AS task
             WHERE (
               task.task_type IN ('call', 'email')
               AND (
                 task.lead_id IS NULL
                 OR (
                   SELECT count(*)
                     FROM leads AS lead
                    WHERE lead.workspace_id = task.workspace_id
                      AND lead.account_id = task.account_id
                 ) <> 1
                 OR NOT EXISTS (
                   SELECT 1
                     FROM leads AS lead
                    WHERE lead.workspace_id = task.workspace_id
                      AND lead.account_id = task.account_id
                      AND lead.id = task.lead_id
                 )
               )
             ) OR (
               task.task_type NOT IN ('call', 'email')
               AND task.lead_id IS NOT NULL
             )
          ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
              MESSAGE = 'cannot safely downgrade pipeline operations';
          END IF;
        END $$
        """
    )
    op.drop_index("ix_tasks_workspace_lead_status_due", table_name="tasks")
    op.drop_constraint("fk_tasks_workspace_account_lead", "tasks", type_="foreignkey")
    op.drop_constraint("ck_tasks_lead_context", "tasks", type_="check")
    op.drop_constraint(
        "ck_activities_outcome_code_nonblank", "activities", type_="check"
    )
    op.drop_column("activities", "outcome_code")
    op.drop_column("tasks", "lead_id")
