"""add_external_id

Revision ID: 0002aaaa
Revises: 0001aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0002aaaa"
down_revision = "0001aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("docs_signature", sa.Column("external_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("docs_signature", "external_id")

