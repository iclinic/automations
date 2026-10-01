from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0011_alter_field_shortens_column"),
    ]

    operations = [
        migrations.AlterField(
            model_name="item",
            name="sku",
            field=models.CharField(help_text="Codigo interno", max_length=16),
        ),
    ]
