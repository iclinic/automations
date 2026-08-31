from django.db import migrations

TABLE = "residue_orphan"


class Migration(migrations.Migration):

    dependencies = []

    operations = [
        migrations.RunSQL(
            sql=f"ALTER TABLE {TABLE} DROP COLUMN code;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
