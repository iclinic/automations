from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("ledger", "0042_modify_supplier_external_id_to_non_null"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="supplierservice",
                    name="external_id",
                    field=models.BigIntegerField(blank=True, editable=False),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "ALTER TABLE ledger_supplierservice "
                        "MODIFY external_id BIGINT NOT NULL, ALGORITHM=INPLACE, LOCK=NONE;",
                    ],
                    reverse_sql=[
                        "ALTER TABLE ledger_supplierservice "
                        "MODIFY external_id BIGINT NULL, ALGORITHM=INPLACE, LOCK=NONE;",
                    ],
                ),
            ],
        ),
    ]
