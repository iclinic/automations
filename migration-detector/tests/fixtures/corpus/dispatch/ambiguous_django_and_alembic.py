# Declara os marcadores dos dois stacks de `.py` ao mesmo tempo.
from alembic import op
from django.db import migrations


class Migration(migrations.Migration):
    operations = []


def upgrade():
    op.drop_column("ledger_entry", "memo")
