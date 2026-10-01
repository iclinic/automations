"""drop_index_and_column

Revision ID: 0005aaaa
Revises: 0004aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0005aaaa"
down_revision = "0004aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_docs_signature_document_id", table_name="docs_signature_v2")
    op.drop_constraint("uq_docs_signature_external_id", "docs_signature_v2", type_="unique")
    op.drop_column("docs_signature_v2", "external_id")


def downgrade() -> None:
    op.add_column("docs_signature_v2", sa.Column("external_id", sa.String(length=36), nullable=True))

