from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0004_indexes_and_constraints"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="item",
            options={"ordering": ("title",), "verbose_name": "Item"},
        ),
        migrations.AlterOrderWithRespectTo(
            name="bundle",
            order_with_respect_to="slug",
        ),
    ]
