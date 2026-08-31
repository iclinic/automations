"""alter_column_undecidable

Revision ID: 0009aaaa
Revises: 0008aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0009aaaa"
down_revision = "0008aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `alter_column` carrega só o estado final, e aqui nem tipo nem nulidade
    # estão escritos: não há como dizer se a coluna encurtou, se passou a NOT
    # NULL ou se mudou só o comentário. É a linha crua da tabela.
    op.alter_column("docs_signature_v2", "checksum", comment="soma de verificação")


def downgrade() -> None:
    op.alter_column("docs_signature_v2", "checksum", comment=None)
