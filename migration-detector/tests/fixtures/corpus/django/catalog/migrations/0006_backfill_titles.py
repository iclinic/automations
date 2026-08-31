from django.db import migrations


def backfill(apps, schema_editor):
    Item = apps.get_model("catalog", "Item")
    Item.objects.filter(title="").update(title="sem titulo")


def unbackfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0005_model_options"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
