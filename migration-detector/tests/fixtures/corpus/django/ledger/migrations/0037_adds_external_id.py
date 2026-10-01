from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplier",
            name="external_id",
            field=models.BigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="supplierservice",
            name="external_id",
            field=models.BigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="supplier",
            constraint=models.UniqueConstraint(
                fields=("external_id",), name="unique_supplier_external_id"
            ),
        ),
    ]
