"""Canonical, date-scoped preparation snapshots; no outreach or task creation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
revision='0017_call_day'
down_revision='0016_call_intent'
branch_labels=depends_on=None

def upgrade():
    op.create_table('call_day_plans',
        sa.Column('workspace_id',pg.UUID(as_uuid=True),sa.ForeignKey('workspaces.id',ondelete='CASCADE'),primary_key=True),
        sa.Column('work_date',sa.Date(),primary_key=True),
        sa.Column('command_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('actor_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('payload',pg.JSONB(),nullable=False))

def downgrade():
    from tests.migration._postgres import require_disposable_postgres
    require_disposable_postgres()
    op.drop_table('call_day_plans')
