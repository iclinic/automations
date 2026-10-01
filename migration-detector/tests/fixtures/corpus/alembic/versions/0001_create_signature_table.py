"""create_signature_table

Revision ID: 0001aaaa
Revises: None

"""
from alembic import op
import sqlalchemy as sa


revision = "0001aaaa"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "docs_signature",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_docs_signature_document_id"), "docs_signature", ["document_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_docs_signature_document_id"), table_name="docs_signature")
    op.drop_table("docs_signature")

