"""alter_and_rename

Revision ID: 0004aaaa
Revises: 0003aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0004aaaa"
down_revision = "0003aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "docs_signature",
        "document_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        nullable=False,
    )
    op.rename_table("docs_signature", "docs_signature_v2")


def downgrade() -> None:
    op.rename_table("docs_signature_v2", "docs_signature")

