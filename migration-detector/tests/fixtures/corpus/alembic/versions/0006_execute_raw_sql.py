"""execute_raw_sql

Revision ID: 0006aaaa
Revises: 0005aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0006aaaa"
down_revision = "0005aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE docs_signature_v2 ADD COLUMN checksum varchar(64) NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE docs_signature_v2 DROP COLUMN checksum")

