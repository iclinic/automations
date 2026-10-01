"""execute_dynamic_sql

Revision ID: 0008aaaa
Revises: 0007aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0008aaaa"
down_revision = "0007aaaa"
branch_labels = None
depends_on = None

TABLE = "docs_webhook_signature"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN payload")


def downgrade() -> None:
    pass

