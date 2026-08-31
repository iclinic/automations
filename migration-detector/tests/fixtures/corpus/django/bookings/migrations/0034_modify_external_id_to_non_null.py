from django.db import migrations, models


def set_not_null_if_needed(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE bookings_booking "
            "MODIFY external_id BIGINT NOT NULL, ALGORITHM=INPLACE, LOCK=NONE"
        )


def set_null_if_needed(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE bookings_booking "
            "MODIFY external_id BIGINT NULL, ALGORITHM=INPLACE, LOCK=NONE"
        )


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("bookings", "0030_add_external_id_and_index"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="booking",
                    name="external_id",
                    field=models.BigIntegerField(blank=True, editable=False),
                ),
            ],
            database_operations=[
                migrations.RunPython(set_not_null_if_needed, set_null_if_needed),
            ],
        ),
    ]
