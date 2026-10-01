from django.db import migrations, models


def _is_nullable(cursor):
    cursor.execute(
        """
        SELECT IS_NULLABLE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'bookings_booking'
          AND COLUMN_NAME = 'external_id'
        """
    )
    row = cursor.fetchone()
    return row is not None and row[0] == "YES"


def _modify_column(cursor, null_clause):
    cursor.execute(
        """
        SET @orig_sql_mode = @@SESSION.sql_mode,
            SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_TRANS_TABLES')
        """
    )
    try:
        cursor.execute(
            f"ALTER TABLE bookings_booking "
            f"MODIFY external_id BIGINT {null_clause}, ALGORITHM=INPLACE, LOCK=NONE"
        )
    finally:
        cursor.execute("SET SESSION sql_mode = @orig_sql_mode")


def set_not_null_if_needed(apps, schema_editor):
    # O pod pode morrer depois do ALTER e antes do registro da migração: se a
    # coluna já estiver NOT NULL, não há nada a fazer.
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        if not _is_nullable(cursor):
            return
        _modify_column(cursor, "NOT NULL")


def set_null_if_needed(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        if _is_nullable(cursor):
            return
        _modify_column(cursor, "NULL")


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
