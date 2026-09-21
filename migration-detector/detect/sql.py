"""Classificador de DDL para MySQL e PostgreSQL.

Núcleo comum das três stacks consumidoras: o `queryRunner.query()` do TypeORM, o
`migrations.RunSQL` do Django e o `op.execute()` do Alembic caem todos aqui. Os dois dialetos convivem no mesmo mapeamento porque a diferença
entre eles é vocabulário — `MODIFY col ... NOT NULL` no MySQL, `ALTER COLUMN col
SET NOT NULL` no PostgreSQL — e não gramática.

A regra que sustenta o módulo: verbo que não está no mapeamento devolve
`unknown`, nunca um palpite. Um `unknown` visível no Slack é barato; uma
classificação errada com cara de certa é o defeito que este classificador
existe para corrigir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable

from .severity import DUPLICATE, Finding, Severity, ref, worst

__all__ = ["classify_sql", "classify_statement", "looks_like_ddl", "split_statements"]

# ---------------------------------------------------------------------------
# Varredura léxica: literais, identificadores citados e comentários
# ---------------------------------------------------------------------------

_QUOTES = "'\"`"
_MASK_CHAR = "~"


def _spans(sql: str) -> list[tuple[str, int, int]]:
    """Trechos que o classificador não pode ler como SQL.

    Devolve `(tipo, inicio, fim)` para cada literal, identificador citado e
    comentário. Aspas dobradas (`''`, `""`) escapam a si mesmas, como manda o
    padrão; barra invertida não escapa nada.
    """
    spans: list[tuple[str, int, int]] = []
    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        if char in _QUOTES:
            j = i + 1
            while j < n:
                if sql[j] == char:
                    if j + 1 < n and sql[j + 1] == char:
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            spans.append(("quoted", i, end))
            i = end
        elif sql.startswith("--", i):
            newline = sql.find("\n", i)
            end = n if newline == -1 else newline
            spans.append(("comment", i, end))
            i = end
        elif sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            end = n if close == -1 else close + 2
            spans.append(("comment", i, end))
            i = end
        else:
            i += 1
    return spans


def _blank_out(sql: str, spans: list[tuple[str, int, int]], filler: str) -> str:
    """Substitui os trechos por `filler`, preservando o comprimento original."""
    chars = list(sql)
    for _, start, end in spans:
        for k in range(start, end):
            chars[k] = filler
    return "".join(chars)


def _strip_comments(sql: str) -> str:
    """Apaga comentários, preservando índices para o resto do módulo."""
    comments = [s for s in _spans(sql) if s[0] == "comment"]
    return _blank_out(sql, comments, " ")


# Verbo de DDL seguido do objeto que ele opera, com espaço para os
# qualificadores que cabem no meio — `CREATE UNIQUE INDEX`, `DROP TABLE IF
# EXISTS`, `CREATE OR REPLACE VIEW`. O objeto é obrigatório porque o verbo
# sozinho não distingue um statement de uma frase: "DROP everything is not what
# this does" começa com `DROP` e é uma mensagem de erro.
_DDL_VERBS = "ALTER|CREATE|DROP|RENAME|TRUNCATE"
_DDL_OBJECTS = (
    "TABLE|COLUMN|INDEX|TYPE|SEQUENCE|SCHEMA|VIEW|CONSTRAINT|DATABASE|TRIGGER|FUNCTION|EXTENSION"
)
_DDL_HEAD = re.compile(
    rf"\s*(?:(?:{_DDL_VERBS})\s+(?:\w+\s+){{0,3}}?(?:{_DDL_OBJECTS})\b|COMMENT\s+ON\b)",
    re.IGNORECASE,
)


def looks_like_ddl(sql: str) -> bool:
    """A string tem cara de statement de DDL?

    Portão de entrada para texto que não veio de um arquivo `.sql` nem de uma
    chamada que promete SQL: o corpo de um `RunPython` do Django chega aqui com
    todas as strings do arquivo dentro, mensagem de erro e nome de coluna
    incluídos. Mandar tudo para `classify_statement` faria cada uma delas
    voltar `unknown`, e todo `RunPython` com texto no corpo viraria pedido de
    revisão manual.

    Responder `True` não promete que `classify_statement` vai reconhecer o
    statement — `ALTER TABLE ... CLUSTER ON` tem cara de DDL e não tem regra.
    Essa é a diferença entre "isto é SQL de schema" e "eu sei o que este SQL
    faz", e é ela que separa um `unknown` honesto de uma string ignorada.
    """
    return bool(_DDL_HEAD.match(_strip_comments(sql)))


def _mask(sql: str) -> str:
    """Neutraliza literais e identificadores citados, preservando índices.

    O texto mascarado é o único que passa pelos regexes de palavra-chave. Sem
    isso, uma constraint chamada `unique_bill_charge_method` viraria uma
    constraint UNIQUE, e o `DROP INDEX` dentro de um `SET @query = '...'` viraria
    um DDL que ninguém vai executar.
    """
    quoted = [s for s in _spans(sql) if s[0] == "quoted"]
    return _blank_out(sql, quoted, _MASK_CHAR)


# ---------------------------------------------------------------------------
# Identificadores
# ---------------------------------------------------------------------------

_IDENT = r'(?:"[^"]*"|`[^`]*`|[A-Za-z_@#][\w$]*)'
_QUALIFIED = re.compile(rf"\s*({_IDENT}(?:\s*\.\s*{_IDENT})*)")


def _identifier_at(raw: str, pos: int) -> tuple[str, int]:
    """Nome do objeto que começa em `pos`, sem as aspas, e onde ele termina."""
    match = _QUALIFIED.match(raw, pos)
    if not match:
        return "", pos
    parts = re.findall(_IDENT, match.group(1))
    return ".".join(part.strip('"`') for part in parts), match.end()


def _ident_span_after(masked: str, raw: str, pattern: str, start: int = 0) -> tuple[str, int]:
    """Nome do objeto depois de `pattern`, e o índice em que ele termina.

    A palavra-chave é procurada no texto mascarado e o nome é lido no texto
    original — os dois têm o mesmo comprimento, então os índices valem para
    ambos. Ler o nome no original é o que permite reconhecer identificadores
    entre aspas, que a máscara apagou de propósito.
    """
    match = re.compile(pattern, re.IGNORECASE).search(masked, start)
    if not match:
        return "", start
    return _identifier_at(raw, match.end())


def _ident_after(masked: str, raw: str, pattern: str, start: int = 0) -> str:
    """Nome do objeto que vem logo depois de `pattern`."""
    return _ident_span_after(masked, raw, pattern, start)[0]


_LONG_NUMBER = re.compile(r"\d{4,}")


def _echo(raw: str, limit: int = 120) -> str:
    """Statement em uma linha para o Slack, cortado antes do primeiro valor.

    O corte não é cosmético. Statement não reconhecido cai aqui, `INSERT` e
    `UPDATE` são statements não reconhecidos, e migração de dados no consumidor
    Django/MySQL carrega dado de paciente. A razão vai para um canal do Slack,
    então nenhum valor pode entrar nela.

    São dois cortes porque um valor chega de duas formas. Entre aspas é o caso
    comum. Solto é o que `CALL migrate_subject(12345678900)` faz: sem aspa
    nenhuma, e é um dado de paciente do mesmo jeito. Quatro dígitos seguidos são
    o limiar — `varchar(255)` e `NUMERIC(10,2)` passam inteiros, CPF, CNPJ e id
    não. O que o time de dados precisa ver aqui é o verbo que o classificador
    não entendeu, não o dado que ele carregava.
    """
    number = _LONG_NUMBER.search(raw)
    cut = min(
        next(
            (start for kind, start, _ in _spans(raw) if kind == "quoted" and raw[start] == "'"),
            len(raw),
        ),
        number.start() if number else len(raw),
    )
    text = " ".join(raw[:cut].split())
    elided = cut < len(raw)
    if len(text) > limit:
        text, elided = text[:limit], True
    return f"{text.rstrip()}…" if elided else text


# ---------------------------------------------------------------------------
# Quebra de statements
# ---------------------------------------------------------------------------


def split_statements(sql: str) -> list[str]:
    """Quebra um texto SQL em statements.

    Ignora `;` dentro de literais, de identificadores citados e de comentários,
    descarta os comentários e não devolve fragmentos vazios.
    """
    body = _strip_comments(sql)
    masked = _mask(body)

    statements: list[str] = []
    start = 0
    for position, char in enumerate(masked):
        if char == ";":
            statements.append(body[start:position])
            start = position + 1
    statements.append(body[start:])

    return [s.strip() for s in statements if s.strip()]


# ---------------------------------------------------------------------------
# Ações de ALTER TABLE
# ---------------------------------------------------------------------------

# A ordem das alternativas é a regra de desempate: a alternação do `re` casa a
# primeira que servir na posição, não a mais longa. Ação específica vem antes da
# genérica — `ADD CONSTRAINT` antes de `ADD INDEX` antes de `ADD <coluna>` — e
# mexer na ordem muda a classificação.
_ALTER_TABLE_ACTIONS = re.compile(
    r"""
      (?P<add_constraint>      \bADD \s+ CONSTRAINT \b )
    | (?P<add_unique_index>    \bADD \s+ UNIQUE (?: \s+ (?:INDEX|KEY) )? \b )
    | (?P<add_key>             \bADD \s+ (?: PRIMARY\s+KEY | FOREIGN\s+KEY | CHECK ) \b )
    | (?P<add_index>           \bADD \s+ (?: FULLTEXT\s+ | SPATIAL\s+ )? (?:INDEX|KEY) \b )
    | (?P<add_column>          \bADD \b \s* (?: COLUMN \b \s* )? (?: IF\s+NOT\s+EXISTS \b \s* )? )
    | (?P<drop_constraint>     \bDROP \s+ CONSTRAINT \b )
    | (?P<drop_key>            \bDROP \s+ (?: PRIMARY\s+KEY | FOREIGN\s+KEY | INDEX | KEY | CHECK ) \b )
    | (?P<drop_column>         \bDROP \b (?! \s* (?: DEFAULT | NOT\s+NULL | IDENTITY | EXPRESSION ) \b )
                               \s* (?: COLUMN \b \s* )? (?: IF\s+EXISTS \b \s* )? )
    | (?P<alter_column>        \bALTER \b \s* (?: COLUMN \b \s* )? )
    | (?P<modify>              \bMODIFY \b \s* (?: COLUMN \b \s* )? )
    | (?P<change>              \bCHANGE \b \s* (?: COLUMN \b \s* )? )
    | (?P<rename_column>       \bRENAME \s+ COLUMN \b )
    | (?P<rename_table>        \bRENAME \s+ (?: TO | AS ) \b )
    | (?P<noise>               \b (?: ALGORITHM | LOCK | ENGINE | AUTO_INCREMENT ) \s* = )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ---------------------------------------------------------------------------
# A tabela de regras
# ---------------------------------------------------------------------------
#
# Daqui para baixo é a tabela de severidade: que verbo vale quanto, e por quê.
# É o artefato que a QQ-2162 leva ao time de dados para validar, então é para
# ser lida e editada por quem não conhece o resto do módulo. Uma linha por
# regra; a mecânica de casar texto ficou toda acima.
#
# `reason` é template: `{table}` e `{column}` nas ações de ALTER TABLE, `{name}`
# nos verbos de statement. `suffix` é acrescentado ao `operation`, que sai do
# cabeçalho que a regex casou. `DUPLICATE` vem de `detect/severity.py`: índice
# único escrito à mão e índice único criado pelo ORM são o mesmo risco, e as
# três stacks têm que dizer a mesma frase.



@dataclass(frozen=True)
class _Rule:
    severity: Severity
    reason: str
    suffix: str = ""
    operation: str = ""
    """Rótulo fixo. Vazio significa derivar do cabeçalho que a regex casou.

    Fixar só vale quando o cabeçalho carrega sintaxe em vez de informação: a
    forma dominante no PostgreSQL é `ADD "c" int`, sem a palavra `COLUMN`, e
    derivar dali daria `ADD`, que não diz nada a quem lê o Slack. Onde o
    cabeçalho é a informação — `ADD PRIMARY KEY` contra `ADD INDEX` — deixe
    derivar.
    """


def _finding(rule: _Rule, operation: str, **names: str) -> Finding:
    label = rule.operation or operation
    return Finding(
        rule.severity,
        f"{label} {rule.suffix}" if rule.suffix else label,
        rule.reason.format(**{key: ref(value) for key, value in names.items()}),
    )


# --- ALTER COLUMN ----------------------------------------------------------
#
# A ordem desta tabela não é significativa: os cinco padrões são mutuamente
# exclusivos, então nenhuma cláusula casa dois. Pode reordenar à vontade.

_ALTER_COLUMN_RULES: tuple[tuple[str, str, _Rule], ...] = (
    ("SET NOT NULL", r"\bSET\s+NOT\s+NULL\b", _Rule(
        Severity.BREAKING,
        "Coluna {column} de {table} passou a NOT NULL — insert sem valor passa a falhar.",
    )),
    ("DROP NOT NULL", r"\bDROP\s+NOT\s+NULL\b", _Rule(
        Severity.CONTROLLED,
        "Coluna {column} de {table} passou a aceitar NULL.",
    )),
    ("TYPE", r"\b(?:SET\s+DATA\s+)?TYPE\b", _Rule(
        Severity.BREAKING,
        "Tipo da coluna {column} de {table} alterado — quem lê o tipo antigo quebra.",
    )),
    ("SET DEFAULT", r"\bSET\s+DEFAULT\b", _Rule(
        Severity.CONTROLLED,
        "Default da coluna {column} de {table} alterado.",
    )),
    ("DROP DEFAULT", r"\bDROP\s+DEFAULT\b", _Rule(
        Severity.CONTROLLED,
        "Default da coluna {column} de {table} alterado.",
    )),
)


# --- ações de ALTER TABLE --------------------------------------------------
#
# A chave é o nome do grupo em `_ALTER_TABLE_ACTIONS`. Grupo sem entrada aqui
# vira `unknown`, e `TestActionTableCoverage` falha antes de chegar a produção.

_ALTER_TABLE_RULES: dict[str, _Rule] = {
    "add_constraint": _Rule(Severity.CONTROLLED, "Constraint adicionada em {table}."),
    "add_unique_index": _Rule(
        Severity.BREAKING, "Índice único adicionado em {table} — " + DUPLICATE
    ),
    "add_key": _Rule(Severity.CONTROLLED, "Constraint adicionada em {table}."),
    "add_index": _Rule(Severity.SAFE, "Índice adicionado em {table}."),
    "add_column": _Rule(
        Severity.SAFE, "Coluna {column} adicionada em {table}.", operation="ADD COLUMN"
    ),
    "drop_constraint": _Rule(Severity.CONTROLLED, "Constraint removida de {table}."),
    "drop_key": _Rule(Severity.CONTROLLED, "Índice ou chave removido de {table}."),
    "drop_column": _Rule(
        Severity.BREAKING,
        "Coluna {column} removida de {table} — quem lê essa coluna quebra.",
        operation="DROP COLUMN",
    ),
    "alter_column": _Rule(
        Severity.UNKNOWN,
        "Ação de ALTER COLUMN não reconhecida sobre {column} de {table} — "
        "precisa de revisão manual.",
    ),
    "modify": _Rule(
        Severity.BREAKING,
        "Tipo da coluna {column} de {table} alterado — quem lê o tipo antigo quebra.",
    ),
    "change": _Rule(
        Severity.BREAKING,
        "Coluna {column} de {table} renomeada e redefinida — quem lê o nome antigo quebra.",
    ),
    "rename_column": _Rule(
        Severity.BREAKING,
        "Coluna {column} de {table} renomeada — quem lê o nome antigo quebra.",
    ),
    "rename_table": _Rule(
        Severity.BREAKING, "Tabela {table} renomeada — quem lê o nome antigo quebra."
    ),
}


# --- refinamentos ----------------------------------------------------------
#
# As quatro regras acima que não dependem só do verbo. Cada uma recebe a
# cláusula mascarada e devolve a regra que substitui a da tabela, ou `None`
# para manter a da tabela.

def _refine_add_constraint(clause: str) -> _Rule | None:
    if re.search(r"\bUNIQUE\b", clause, re.I):
        return _Rule(
            Severity.BREAKING,
            "Constraint UNIQUE adicionada em {table} — " + DUPLICATE,
            "... UNIQUE",
        )
    return None


def _refine_add_column(clause: str) -> _Rule | None:
    # Mesma regra que `django.py` aplica ao `AddField` e `alembic.py` ao
    # `add_column`: sem `NULL` e sem `DEFAULT`, a coluna não entra numa tabela
    # que já tem linha. O DDL escrito à mão tem que classificar igual ao
    # equivalente em ORM, senão a mesma mudança sai com duas cores.
    if re.search(r"\bNOT\s+NULL\b", clause, re.I) and not re.search(r"\bDEFAULT\b", clause, re.I):
        return _Rule(
            Severity.CONTROLLED,
            "Coluna {column} adicionada em {table} como NOT NULL sem DEFAULT — "
            "tabela que já tem linha impede a migração.",
            operation="ADD COLUMN",
        )
    return None


def _refine_modify(clause: str) -> _Rule | None:
    if re.search(r"\bNOT\s+NULL\b", clause, re.I):
        return _Rule(
            Severity.BREAKING,
            "Coluna {column} de {table} passou a NOT NULL — insert sem valor passa a falhar.",
            "... NOT NULL",
        )
    return None


def _refine_alter_column(clause: str) -> _Rule | None:
    for subaction, pattern, rule in _ALTER_COLUMN_RULES:
        if re.search(pattern, clause, re.I):
            return replace(rule, suffix=f"... {subaction}")
    return None


_ALTER_TABLE_REFINERS: dict[str, Callable[[str], _Rule | None]] = {
    "add_constraint": _refine_add_constraint,
    "add_column": _refine_add_column,
    "modify": _refine_modify,
    "alter_column": _refine_alter_column,
}


def _classify_alter_table_clause(
    kind: str, operation: str, table: str, column: str, clause: str
) -> Finding:
    """Severidade de uma ação de ALTER TABLE.

    `operation` vem do cabeçalho que a regex casou, então descreve o que o
    statement realmente diz. `clause` vai da ação até a próxima, mascarada, e é
    o que os refinamentos leem.
    """
    rule = _ALTER_TABLE_RULES.get(kind)
    if rule is None:
        # Grupo novo na regex de ações sem regra correspondente. Devolver
        # `unknown` em vez de estourar: quem mexer na tabela erra num teste, não
        # numa action rodando em PR.
        return Finding(
            Severity.UNKNOWN,
            operation,
            f"Ação `{kind}` do ALTER TABLE sem regra no classificador — precisa de revisão manual.",
        )

    refiner = _ALTER_TABLE_REFINERS.get(kind)
    if refiner is not None:
        rule = refiner(clause) or rule
    return _finding(rule, operation, table=table, column=column)


def _classify_alter_table(masked: str, raw: str) -> Finding:
    table, actions_start = _ident_span_after(
        masked, raw, r"\bALTER\s+TABLE\b\s*(?:\bONLY\b\s*)?(?:\bIF\s+EXISTS\b\s*)?"
    )
    matches = list(_ALTER_TABLE_ACTIONS.finditer(masked, actions_start))

    findings: list[Finding] = []
    for index, match in enumerate(matches):
        kind = match.lastgroup
        if kind == "noise":
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(masked)
        findings.append(
            _classify_alter_table_clause(
                kind,
                _operation_of(match),
                table,
                _identifier_at(raw, match.end())[0],
                masked[match.start():end],
            )
        )

    if not findings:
        return Finding(
            Severity.UNKNOWN,
            "ALTER TABLE",
            f"Nenhuma ação reconhecida no ALTER TABLE de {ref(table)} — "
            "precisa de revisão manual.",
        )
    return worst(findings)


# --- verbos de statement ---------------------------------------------------
#
# Avaliados na ordem desta tabela; o primeiro cabeçalho que casar decide.
#
# `head` é só o teste de reconhecimento, e é mínimo de propósito: nada de
# sintaxe opcional nele. Duas razões. Uma, `head` também é de onde sai o
# `operation`, e `DROP TABLE IF EXISTS` como rótulo faria a QQ-2158 agrupar a
# mesma operação em dois baldes. Duas, e pior, qualquer `\s+` obrigatório no
# fim do cabeçalho faz o verbo deixar de casar quando não há espaço —
# `DROP TABLE"orders"` é PostgreSQL válido.
#
# Onde achar o nome do objeto é a outra metade, e mora em `name_after`, um
# padrão procurado a partir do fim do cabeçalho: sintaxe opcional a pular
# (`IF EXISTS`, `CONCURRENTLY`) ou outro lugar onde o nome está (`CREATE INDEX`
# guarda a tabela depois do `ON`). Sem `name_after`, o nome vem logo depois do
# cabeçalho — `_identifier_at` já engole o espaço, se houver.
#
# `ALTER TABLE` não está aqui: é o único verbo que devolve a pior de várias
# ações, e tem função própria.

def _refine_create_index(masked: str) -> _Rule | None:
    if re.match(r"\s*CREATE\s+UNIQUE\b", masked, re.I):
        return _Rule(Severity.BREAKING, "Índice único criado em {name} — " + DUPLICATE)
    return None


def _refine_alter_type(masked: str) -> _Rule | None:
    if re.search(r"\bADD\s+VALUE\b", masked, re.I):
        return _Rule(Severity.SAFE, "Novo valor adicionado ao enum {name}.", "... ADD VALUE")
    return None


@dataclass(frozen=True)
class _Statement:
    head: str
    rule: _Rule
    name_after: str = ""
    refine: Callable[[str], _Rule | None] | None = None


_STATEMENT_RULES: tuple[_Statement, ...] = (
    _Statement(
        r"(?:START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK)\b",
        _Rule(Severity.NONE, "Controle de transação — não altera schema."),
    ),
    _Statement(r"SET\b", _Rule(Severity.NONE, "Ajuste de sessão — não altera schema.")),
    _Statement(
        r"COMMENT\s+ON\b",
        _Rule(Severity.NONE, "Comentário de metadado — não altera schema."),
    ),
    _Statement(
        r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\b",
        _Rule(Severity.SAFE, "Tabela {name} criada."),
        name_after=r"\s*(?:\bIF\s+NOT\s+EXISTS\b\s*)?",
    ),
    _Statement(
        r"DROP\s+TABLE\b",
        _Rule(Severity.BREAKING, "Tabela {name} removida — quem lê essa tabela quebra."),
        name_after=r"\s*(?:\bIF\s+EXISTS\b\s*)?",
    ),
    _Statement(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\b",
        _Rule(Severity.SAFE, "Índice criado em {name}."),
        name_after=r"\bON\b\s*(?:\bONLY\b\s*)?",
        refine=_refine_create_index,
    ),
    _Statement(
        r"DROP\s+INDEX\b",
        _Rule(
            Severity.CONTROLLED,
            "Índice {name} removido — consulta que dependia dele fica mais lenta.",
        ),
        name_after=r"\s*(?:\bCONCURRENTLY\b\s*)?(?:\bIF\s+EXISTS\b\s*)?",
    ),
    _Statement(r"CREATE\s+TYPE\b", _Rule(Severity.SAFE, "Tipo {name} criado.")),
    _Statement(
        r"DROP\s+TYPE\b",
        _Rule(
            Severity.CONTROLLED,
            "Tipo {name} removido — coluna que ainda usa esse tipo impede a migração.",
        ),
        name_after=r"\s*(?:\bIF\s+EXISTS\b\s*)?",
    ),
    _Statement(
        r"ALTER\s+TYPE\b",
        _Rule(
            Severity.UNKNOWN,
            "ALTER TYPE em {name} que não é ADD VALUE — precisa de revisão manual.",
        ),
        refine=_refine_alter_type,
    ),
    _Statement(
        r"CREATE\s+SEQUENCE\b",
        _Rule(Severity.SAFE, "Sequence {name} criada."),
        name_after=r"\s*(?:\bIF\s+NOT\s+EXISTS\b\s*)?",
    ),
)

_ALTER_TABLE_HEAD = re.compile(r"\s*ALTER\s+TABLE\b", re.IGNORECASE)
_COMPILED_STATEMENTS: tuple[tuple[re.Pattern[str], _Statement], ...] = tuple(
    (re.compile(r"\s*" + statement.head, re.IGNORECASE), statement)
    for statement in _STATEMENT_RULES
)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def _operation_of(match: re.Match[str]) -> str:
    """O cabeçalho casado, normalizado — o que o statement de fato diz."""
    return " ".join(match.group().split()).upper()


def classify_statement(sql: str) -> Finding:
    """Classifica um único statement DDL.

    Verbo fora do mapeamento devolve `unknown` com o statement na razão, para o
    time de dados ver o que o classificador não entendeu.
    """
    raw = _strip_comments(sql)
    if not raw.strip():
        if sql.strip():
            return Finding(Severity.NONE, "SQL COMMENT", "Apenas comentário — não altera schema.")
        return Finding(Severity.NONE, "EMPTY STATEMENT", "Statement vazio.")

    masked = _mask(raw)
    if _ALTER_TABLE_HEAD.match(masked):
        return _classify_alter_table(masked, raw)

    for pattern, statement in _COMPILED_STATEMENTS:
        match = pattern.match(masked)
        if match is None:
            continue
        name = (
            _ident_after(masked, raw, statement.name_after, match.end())
            if statement.name_after
            else _identifier_at(raw, match.end())[0]
        )
        rule = (statement.refine and statement.refine(masked)) or statement.rule
        return _finding(rule, _operation_of(match), name=name)

    # `\w` casa dígito, então este verbo já foi um vazamento: um bloco de dados
    # estilo `pg_dump` dentro da migração faz o statement começar pelo primeiro
    # campo da linha do paciente, e o "verbo" saía `4471902` ou `12345678900`
    # direto para o Slack — com a razão corretamente elidida por `_echo` ao
    # lado. `ignore_name_contains: dump` filtra pelo nome do arquivo, então
    # `0042_backfill_subjects.sql` passa reto.
    #
    # Verbo de statement SQL é uma palavra alfabética. O que não for isso não é
    # verbo, é dado, e vira `?` — que é o que `django.py` e `alembic.py` já
    # fazem quando não têm um nome que se possa dizer em voz alta.
    #
    # O portão é o `.isalpha()`, e é só ele: apertar a classe do regex para
    # `[A-Za-z_]\w*` não muda uma resposta sequer atrás dele — `9abc` e
    # `cpf_do_paciente_11122233344` já caem no mesmo `?`. Dois guardas onde um
    # decide tudo deixam o segundo sem teste que o mate.
    verb = re.match(r"\s*(\w+)", masked)
    return Finding(
        Severity.UNKNOWN,
        verb.group(1).upper() if verb and verb.group(1).isalpha() else "?",
        f"Statement não reconhecido pelo classificador: {_echo(raw)} — precisa de revisão manual.",
    )


def classify_sql(sql: str) -> list[Finding]:
    """Classifica cada statement de um texto SQL, na ordem em que aparecem."""
    return [classify_statement(statement) for statement in split_statements(sql)]
