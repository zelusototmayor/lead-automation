"""Durable bounded agent work, independent of human obligations.

The DDL is frozen here so future ORM changes cannot alter this migration.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE agent_work (\n\tid UUID NOT NULL, \n\tworkspace_id UUID NOT NULL, \n\tsource_key VARCHAR(512) NOT NULL, \n\tkind VARCHAR(64) NOT NULL, \n\ttask_id UUID, \n\tlead_id UUID, \n\tpayload JSONB NOT NULL, \n\tstatus VARCHAR(16) DEFAULT 'queued' NOT NULL, \n\tattempts INTEGER DEFAULT 0 NOT NULL, \n\tavailable_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tworker_id VARCHAR(128), \n\tlease_token UUID, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tlast_lease_token UUID, \n\tresult JSONB, \n\tresult_hash CHAR(64), \n\terror VARCHAR(256), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_agent_work PRIMARY KEY (id), \n\tCONSTRAINT uq_agent_work_source UNIQUE (workspace_id, source_key), \n\tCONSTRAINT ck_agent_work_status CHECK (status IN ('queued','running','waiting','completed','failed')), \n\tCONSTRAINT ck_agent_work_attempts CHECK (attempts BETWEEN 0 AND 3), \n\tCONSTRAINT ck_agent_work_lease CHECK ((status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)), \n\tCONSTRAINT fk_agent_work_task FOREIGN KEY(workspace_id, task_id) REFERENCES tasks (workspace_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_agent_work_lead FOREIGN KEY(workspace_id, lead_id) REFERENCES leads (workspace_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_agent_work_workspace_id_workspaces FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE INDEX ix_agent_work_due ON agent_work (workspace_id, status, available_at)"
    )


def downgrade():
    op.drop_table("agent_work")
