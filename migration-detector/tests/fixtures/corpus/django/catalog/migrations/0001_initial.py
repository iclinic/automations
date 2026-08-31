from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ("sku", models.CharField(max_length=64)),
                ("label", models.CharField(max_length=120)),
                ("notes", models.TextField(blank=True, null=True)),
                ("price", models.DecimalField(decimal_places=2, max_digits=10)),
            ],
            options={"ordering": ("sku",)},
        ),
        migrations.CreateModel(
            name="Bundle",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ("slug", models.CharField(max_length=40)),
            ],
        ),
        migrations.CreateModel(
            name="Legacy",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
            ],
        ),
    ]
