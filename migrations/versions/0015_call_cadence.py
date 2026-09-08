"""Add nullable, versioned call facts without legacy backfill.

Live head 0014 verified via deployment's Alembic current before allocation.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015_call_cadence"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("activities", sa.Column("call_details", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint("ck_activities_call_details_v1", "activities", "call_details IS NULL OR (jsonb_typeof(call_details) = 'object' AND call_details ? 'schema_version' AND call_details->'schema_version' = '1'::jsonb)")


def downgrade():
    # Evidence-destructive rollback is only allowed on a guarded disposable DB.
    from tests.migration._postgres import require_disposable_postgres
    require_disposable_postgres()
    op.drop_constraint("ck_activities_call_details_v1", "activities", type_="check")
    op.drop_column("activities", "call_details")
