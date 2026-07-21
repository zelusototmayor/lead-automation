"""Add canonical identity fields for leads before account creation.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


_FIELDS = (
    ("company_name", sa.String(length=512)),
    ("contact_name", sa.String(length=512)),
    ("contact_email", postgresql.CITEXT()),
    ("contact_phone", sa.String(length=64)),
    ("city", sa.String(length=255)),
)


def upgrade() -> None:
    for name, column_type in _FIELDS:
        op.add_column("leads", sa.Column(name, column_type, nullable=True))
        cast = "::text" if name == "contact_email" else ""
        op.create_check_constraint(
            f"ck_leads_{name}_nonblank",
            "leads",
            f"{name} IS NULL OR length(btrim({name}{cast})) > 0",
        )


def downgrade() -> None:
    columns = ", ".join(name for name, _ in _FIELDS)
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM leads WHERE ROW({columns}) IS DISTINCT FROM ROW(NULL, NULL, NULL, NULL, NULL)) THEN "
        "RAISE EXCEPTION 'cannot downgrade while pre-account lead identity exists'; "
        "END IF; END $$"
    )
    for name, _ in reversed(_FIELDS):
        op.drop_constraint(f"ck_leads_{name}_nonblank", "leads", type_="check")
        op.drop_column("leads", name)
