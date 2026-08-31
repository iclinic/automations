from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0008_alter_unique_together"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Archive",
        ),
    ]
