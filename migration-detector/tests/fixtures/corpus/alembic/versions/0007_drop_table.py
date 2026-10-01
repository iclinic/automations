"""drop_table

Revision ID: 0007aaaa
Revises: 0006aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0007aaaa"
down_revision = "0006aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("docs_signature_v2")


def downgrade() -> None:
    op.create_table(
        "docs_signature_v2",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

