from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_add_and_remove_fields"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="Legacy",
            new_name="Archive",
        ),
        migrations.RenameField(
            model_name="item",
            old_name="label",
            new_name="title",
        ),
    ]
