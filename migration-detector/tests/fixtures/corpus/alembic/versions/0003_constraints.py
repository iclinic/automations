"""constraints

Revision ID: 0003aaaa
Revises: 0002aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0003aaaa"
down_revision = "0002aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_docs_signature_external_id", "docs_signature", ["external_id"])
    op.create_primary_key("pk_docs_signature", "docs_signature", ["id"])
    op.create_foreign_key(
        "fk_docs_signature_owner", "docs_signature", "docs_owner", ["owner_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_docs_signature_external_id", "docs_signature", type_="unique")

