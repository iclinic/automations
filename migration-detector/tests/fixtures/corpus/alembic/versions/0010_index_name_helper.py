"""index_name_helper

Revision ID: 0010aaaa
Revises: 0009aaaa

"""
from alembic import op
import sqlalchemy as sa


revision = "0010aaaa"
down_revision = "0009aaaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `op.f()` não é operação: é o helper que devolve o nome do índice. A linha
    # da tabela existe para que uma chamada dele no corpo de `upgrade()` não
    # seja confundida com uma operação que o classificador não conhece — que
    # sairia `unknown` e mandaria para revisão manual uma migração que não
    # altera schema nenhum.
    op.f("ix_docs_signature_checksum")
    op.create_index("ix_docs_signature_checksum", "docs_signature_v2", ["checksum"])


def downgrade() -> None:
    op.drop_index("ix_docs_signature_checksum", table_name="docs_signature_v2")
