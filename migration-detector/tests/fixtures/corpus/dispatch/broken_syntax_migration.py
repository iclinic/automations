# ATENCAO: ESTE ARQUIVO NAO COMPILA, E NAO PODE COMPILAR.
#
# Ele e a fixture da linha `NOT_PYTHON` do dispatch, e so prova o que tem que
# provar sendo um erro de sintaxe de verdade. `# ruff: noqa` nao resolve: nao e
# uma queixa de lint, e o parser que nao chega ao fim do arquivo.
#
# Todo passo futuro que compile ou analise o repositorio inteiro — `ruff`,
# `flake8`, `mypy`, `python -m compileall` — precisa excluir
# `migration-detector/tests/fixtures/corpus/`. Hoje nao ha nenhum configurado,
# entao nada quebra; o primeiro a entrar quebra aqui.
#
# Arquivo que nao e Python valido. Um classificador que so responde ao que
# parseia teria devolvido lista vazia aqui, e lista vazia le-se como "nada a
# reportar".
from django.db import migrations


class Migration(migrations.Migration
    operations = [
