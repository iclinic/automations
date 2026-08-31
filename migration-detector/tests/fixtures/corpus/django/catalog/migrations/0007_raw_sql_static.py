from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0006_backfill_titles"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE catalog_item DROP COLUMN barcode;",
            reverse_sql="ALTER TABLE catalog_item ADD COLUMN barcode varchar(32) NULL;",
        ),
    ]
