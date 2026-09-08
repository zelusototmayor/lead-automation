"""Add optional call scheduling intent without changing legacy obligations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "0016_call_intent"
down_revision = "0015_call_cadence"
branch_labels = depends_on = None

def upgrade():
    op.add_column("tasks", sa.Column("call_intent", postgresql.JSONB(none_as_null=True), nullable=True))

def downgrade():
    from tests.migration._postgres import require_disposable_postgres
    require_disposable_postgres()
    op.drop_column("tasks", "call_intent")
