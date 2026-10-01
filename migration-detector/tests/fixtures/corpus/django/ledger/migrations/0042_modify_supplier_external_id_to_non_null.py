from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("ledger", "0037_adds_external_id"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="supplier",
                    name="external_id",
                    field=models.BigIntegerField(blank=True, editable=False),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "SET @orig_lock_wait_timeout = @@SESSION.lock_wait_timeout;",
                        "SET SESSION lock_wait_timeout = 60;",
                        "ALTER TABLE ledger_supplier "
                        "MODIFY external_id BIGINT NOT NULL, ALGORITHM=INPLACE, LOCK=NONE;",
                        "SET SESSION lock_wait_timeout = @orig_lock_wait_timeout;",
                    ],
                    reverse_sql=[
                        "ALTER TABLE ledger_supplier "
                        "MODIFY external_id BIGINT NULL, ALGORITHM=INPLACE, LOCK=NONE;",
                    ],
                ),
            ],
        ),
    ]
