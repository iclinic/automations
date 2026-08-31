from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_rename_model_and_field"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="item",
            index=models.Index(fields=["sku"], name="item_sku_idx"),
        ),
        migrations.RemoveIndex(
            model_name="item",
            name="item_sku_idx",
        ),
        migrations.AddConstraint(
            model_name="item",
            constraint=models.CheckConstraint(
                check=models.Q(stock__gte=0), name="item_stock_non_negative"
            ),
        ),
        migrations.RemoveConstraint(
            model_name="item",
            name="item_stock_non_negative",
        ),
    ]
