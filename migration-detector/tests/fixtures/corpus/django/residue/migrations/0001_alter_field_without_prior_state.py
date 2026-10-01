from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = []

    operations = [
        migrations.AlterField(
            model_name="orphan",
            name="code",
            field=models.CharField(max_length=32),
        ),
    ]
