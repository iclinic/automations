# O ramo de `SeparateDatabaseAndState` montado fora da chamada. A linha da
# tabela existe para isto: o classificador não tem como saber quais operações
# a lista carrega, e adivinhar aqui esconderia o `RunSQL` que estivesse dentro.
from django.db import migrations, models

STATE_OPERATIONS = [
    migrations.AlterField(
        model_name="orphan",
        name="code",
        field=models.CharField(max_length=32),
    ),
]


class Migration(migrations.Migration):

    dependencies = []

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=STATE_OPERATIONS,
            database_operations=[],
        ),
    ]
