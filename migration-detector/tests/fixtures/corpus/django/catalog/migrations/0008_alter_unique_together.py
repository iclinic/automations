from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0007_raw_sql_static"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="item",
            unique_together={("sku", "title")},
        ),
    ]
