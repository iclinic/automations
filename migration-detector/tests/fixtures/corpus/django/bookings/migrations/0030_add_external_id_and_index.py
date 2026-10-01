from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="external_id",
            field=models.BigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(fields=["external_id"], name="booking_external_id_idx"),
        ),
    ]
