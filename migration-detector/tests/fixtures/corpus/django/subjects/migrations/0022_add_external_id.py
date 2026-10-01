from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("subjects", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="subject",
            name="external_id",
            field=models.BigIntegerField(blank=True, editable=False, null=True),
        ),
    ]
