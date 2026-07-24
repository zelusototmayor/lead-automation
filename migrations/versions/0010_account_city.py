"""Add canonical account city for operational search.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("city", sa.String(length=255), nullable=True))
    op.create_check_constraint(
        "ck_accounts_city_nonblank",
        "accounts",
        "city IS NULL OR length(btrim(city)) > 0",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM accounts WHERE city IS NOT NULL) THEN "
        "RAISE EXCEPTION 'cannot downgrade while canonical account city exists'; "
        "END IF; END $$"
    )
    op.drop_constraint("ck_accounts_city_nonblank", "accounts", type_="check")
    op.drop_column("accounts", "city")
