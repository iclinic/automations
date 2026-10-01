from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="barcode",
            field=models.CharField(max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="stock",
            field=models.IntegerField(),
        ),
        migrations.RemoveField(
            model_name="item",
            name="notes",
        ),
    ]
