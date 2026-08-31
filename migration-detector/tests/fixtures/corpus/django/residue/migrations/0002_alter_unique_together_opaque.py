from django.db import migrations

UNIQUE_SETS = [("code", "kind")]


class Migration(migrations.Migration):

    dependencies = []

    operations = [
        migrations.AlterUniqueTogether(
            name="orphan",
            unique_together=UNIQUE_SETS,
        ),
    ]
