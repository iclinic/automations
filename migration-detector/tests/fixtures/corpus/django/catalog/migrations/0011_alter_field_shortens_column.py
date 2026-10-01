from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0010_merge_20260101_0000"),
    ]

    operations = [
        migrations.AlterField(
            model_name="item",
            name="sku",
            field=models.CharField(max_length=16),
        ),
    ]
