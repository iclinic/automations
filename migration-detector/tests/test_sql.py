"""Testes do classificador de DDL.

Os statements usados aqui foram extraídos do corpus real dos três repositórios
consumidores — um Django/MySQL, um Alembic/PostgreSQL e um TypeORM/PostgreSQL.
Statements inventados aparecem só onde o corpus não cobre o verbo exigido pelo
aceite.
"""

import string

import pytest

from detect.severity import MANUAL, Severity
from detect.sql import (
    _ALTER_COLUMN_RULES,
    _ALTER_TABLE_ACTIONS,
    _ALTER_TABLE_REFINERS,
    _ALTER_TABLE_RULES,
    _STATEMENT_RULES,
    _classify_alter_table_clause,
    classify_sql,
    classify_statement,
    looks_like_ddl,
    split_statements,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def severity_of(sql: str) -> Severity:
    return classify_statement(sql).severity


# ---------------------------------------------------------------------------
# Quebra de statements
# ---------------------------------------------------------------------------


class TestSplitStatements:
    def test_single_statement_without_semicolon(self):
        assert split_statements('ALTER TABLE "schedule" DROP COLUMN "status"') == [
            'ALTER TABLE "schedule" DROP COLUMN "status"'
        ]

    def test_splits_on_semicolon(self):
        sql = "ALTER TABLE a DROP COLUMN b; ALTER TABLE c DROP COLUMN d;"
        assert split_statements(sql) == [
            "ALTER TABLE a DROP COLUMN b",
            "ALTER TABLE c DROP COLUMN d",
        ]

    def test_ignores_blank_fragments(self):
        assert split_statements(";;  ;\n;") == []

    def test_ignores_trailing_semicolon(self):
        assert split_statements("DROP TABLE orders;") == ["DROP TABLE orders"]

    def test_does_not_split_on_semicolon_inside_a_string_literal(self):
        sql = "SET SESSION sql_mode = 'a;b'; DROP TABLE orders;"
        assert split_statements(sql) == [
            "SET SESSION sql_mode = 'a;b'",
            "DROP TABLE orders",
        ]

    def test_does_not_split_on_semicolon_inside_a_quoted_identifier(self):
        sql = 'ALTER TABLE "we;ird" DROP COLUMN "x"; DROP TABLE orders'
        assert split_statements(sql) == [
            'ALTER TABLE "we;ird" DROP COLUMN "x"',
            "DROP TABLE orders",
        ]

    def test_does_not_split_on_semicolon_inside_a_backquoted_identifier(self):
        sql = "ALTER TABLE `we;ird` DROP COLUMN `x`; DROP TABLE orders"
        assert split_statements(sql) == [
            "ALTER TABLE `we;ird` DROP COLUMN `x`",
            "DROP TABLE orders",
        ]

    def test_handles_doubled_quote_escape_inside_a_literal(self):
        sql = "SET @x = 'it''s; fine'; DROP TABLE orders"
        assert split_statements(sql) == ["SET @x = 'it''s; fine'", "DROP TABLE orders"]

    def test_drops_line_comments(self):
        sql = "-- legacy indexes\nDROP INDEX ix_a ON t;"
        assert split_statements(sql) == ["DROP INDEX ix_a ON t"]

    def test_drops_block_comments(self):
        sql = "/* remove\n   isso */ DROP TABLE orders;"
        assert split_statements(sql) == ["DROP TABLE orders"]

    def test_keeps_a_semicolon_that_lives_inside_a_comment_from_splitting(self):
        sql = "DROP TABLE a -- e depois; nada\n; DROP TABLE b"
        assert split_statements(sql) == ["DROP TABLE a", "DROP TABLE b"]

    def test_does_not_treat_a_double_dash_inside_a_literal_as_a_comment(self):
        sql = "SET @x = 'a--b'; DROP TABLE orders"
        assert split_statements(sql) == ["SET @x = 'a--b'", "DROP TABLE orders"]

    def test_comment_only_input_yields_nothing(self):
        assert split_statements("-- nada aqui\n/* nem aqui */") == []

    def test_empty_input_yields_nothing(self):
        assert split_statements("") == []
        assert split_statements("   \n  ") == []

    def test_backslash_escapes_a_quote_inside_a_literal(self):
        # MySQL, o banco do consumidor Django, lê `\'` como aspa dentro do
        # literal. Lida como fim de literal, a aspa seguinte abria outro que
        # corria até o fim do texto e levava o `DROP TABLE` junto.
        sql = r"COMMENT ON COLUMN subject.cpf IS 'paciente\'s id'; DROP TABLE subject;"
        assert split_statements(sql) == [
            r"COMMENT ON COLUMN subject.cpf IS 'paciente\'s id'",
            "DROP TABLE subject",
        ]

    def test_an_escaped_backslash_does_not_escape_the_closing_quote(self):
        sql = r"SET @x = 'C:\\'; DROP TABLE orders"
        assert split_statements(sql) == [r"SET @x = 'C:\\'", "DROP TABLE orders"]


# ---------------------------------------------------------------------------
# Texto que termina dentro de um literal ou comentário
# ---------------------------------------------------------------------------


class TestTextThatEndsInsideALiteral:
    # Um literal que não fecha engole o resto da entrada: o que vem depois da
    # aspa some da saída, e o arquivo sai com a severidade do que veio antes.
    # Qualquer desacordo de dialeto sobre onde um literal termina acaba aqui,
    # então a guarda não depende de dialeto.

    @pytest.mark.parametrize(
        "sql",
        (
            "COMMENT ON COLUMN subject.cpf IS 'paciente; DROP TABLE subject;",
            'CREATE INDEX "ix_a; DROP TABLE subject;',
            "CREATE INDEX `ix_a; DROP TABLE subject;",
            "COMMENT ON TABLE subject IS 'x'; /* DROP TABLE subject;",
        ),
        ids=("literal", "double_quoted_identifier", "backquoted_identifier", "block_comment"),
    )
    def test_is_unknown(self, sql):
        findings = classify_sql(sql)
        assert findings[-1].severity is Severity.UNKNOWN
        assert findings[-1].reason.endswith(MANUAL)

    def test_keeps_the_statements_before_the_opening(self):
        findings = classify_sql("DROP TABLE a; COMMENT ON TABLE b IS 'x")
        assert [f.severity for f in findings] == [
            Severity.BREAKING,
            Severity.NONE,
            Severity.UNKNOWN,
        ]

    def test_the_reason_carries_nothing_from_inside_the_literal(self):
        # O literal aberto é justamente o texto que pode ser dado de paciente.
        finding = classify_sql("COMMENT ON TABLE b IS '12345678900 Maria")[-1]
        assert finding.severity is Severity.UNKNOWN
        assert "Maria" not in finding.reason
        assert "12345678900" not in finding.reason

    def test_an_unclosed_line_comment_is_just_a_comment(self):
        # `--` fecha no fim da linha ou no fim do texto; nos dois casos o
        # banco lê o mesmo que o classificador.
        assert [f.severity for f in classify_sql("DROP TABLE a; -- fim")] == [Severity.BREAKING]


# ---------------------------------------------------------------------------
# classify_sql sobre múltiplos statements
# ---------------------------------------------------------------------------


class TestClassifySql:
    def test_returns_one_finding_per_statement(self):
        # migração real do consumidor Django (ledger/0042), com BEGIN/COMMIT implícito
        sql = (
            "ALTER TABLE `ledger_userprofile` ALGORITHM=INPLACE, LOCK=NONE, "
            "ADD COLUMN `otp_enabled` bool DEFAULT b'0' NOT NULL;"
            "ALTER TABLE `ledger_userprofile` ALTER COLUMN `otp_enabled` DROP DEFAULT;"
        )
        findings = classify_sql(sql)
        assert [f.severity for f in findings] == [
            Severity.SAFE,
            Severity.CONTROLLED,
        ]

    def test_empty_string_returns_no_findings(self):
        assert classify_sql("") == []

    def test_comment_only_returns_no_findings(self):
        assert classify_sql("-- só um comentário") == []

    def test_transaction_wrapper_does_not_add_severity(self):
        sql = "BEGIN; ALTER TABLE t ADD COLUMN c varchar(10) NULL; COMMIT;"
        findings = classify_sql(sql)
        assert [f.severity for f in findings] == [
            Severity.NONE,
            Severity.SAFE,
            Severity.NONE,
        ]

    def test_multi_statement_typeorm_migration_from_the_corpus(self):
        # 1576003532524-AutoMigrate.ts, up()
        sql = """
        ALTER TABLE "schedule" RENAME COLUMN "isClosed" TO "status";
        CREATE TYPE "message_tracking_event_enum" AS ENUM('Send', 'Reject');
        CREATE UNIQUE INDEX "IDX_dec" ON "message_tracking" ("event", "messageId");
        ALTER TABLE "schedule" DROP COLUMN "status";
        ALTER TABLE "meeting_recording" ADD CONSTRAINT "UQ_0ed" UNIQUE ("scheduleMeetingId");
        """
        findings = classify_sql(sql)
        assert len(findings) == 5
        assert [f.severity for f in findings] == [
            Severity.BREAKING,
            Severity.SAFE,
            Severity.BREAKING,
            Severity.BREAKING,
            Severity.BREAKING,
        ]


# ---------------------------------------------------------------------------
# Aceite: DROP TABLE, DROP COLUMN e RENAME são breaking
# ---------------------------------------------------------------------------


class TestDestructiveVerbsAreBreaking:
    def test_drop_table(self):
        assert severity_of('DROP TABLE "message_tracking"') is Severity.BREAKING

    def test_drop_table_mysql_quoting(self):
        assert severity_of("DROP TABLE `claim_version`") is Severity.BREAKING

    def test_drop_table_if_exists(self):
        assert severity_of("DROP TABLE IF EXISTS orders") is Severity.BREAKING

    def test_drop_column(self):
        assert (
            severity_of('ALTER TABLE "schedule" DROP COLUMN "suspended"')
            is Severity.BREAKING
        )

    def test_drop_column_mysql(self):
        assert (
            severity_of("ALTER TABLE `bookings_booking` DROP COLUMN `status`")
            is Severity.BREAKING
        )

    def test_rename_column(self):
        assert (
            severity_of(
                'ALTER TABLE "meeting_log" RENAME COLUMN "partyType" TO "memberType"'
            )
            is Severity.BREAKING
        )

    def test_rename_table(self):
        assert (
            severity_of(
                'ALTER TABLE "meeting_participant_joined" RENAME TO "meeting_room"'
            )
            is Severity.BREAKING
        )

    def test_mysql_change_renames_and_retypes_a_column(self):
        assert (
            severity_of(
                "ALTER TABLE `claim_lot` CHANGE `version` `version_id` varchar(8) NOT NULL, "
                "ALGORITHM=COPY, LOCK=SHARED"
            )
            is Severity.BREAKING
        )


# ---------------------------------------------------------------------------
# ADD COLUMN: a mesma regra do AddField do Django e do add_column do Alembic
# ---------------------------------------------------------------------------


class TestAddColumn:
    """Coluna NOT NULL sem default não entra em tabela que já tem linha.

    É a mesma regra que `django.py` aplica ao `AddField` e `alembic.py` ao
    `add_column`. As três stacks passam por aqui quando o DDL é escrito à mão,
    então a regra tem que valer igual nos três caminhos.
    """

    def test_nullable_column_is_safe(self):
        assert (
            severity_of("ALTER TABLE `subjects_subject` ADD COLUMN `unique_ref_code` varchar(47) NULL")
            is Severity.SAFE
        )

    def test_column_without_a_null_clause_is_safe(self):
        assert (
            severity_of("ALTER TABLE `bookings_booking` ADD COLUMN `unique_ref_code` varchar(47)")
            is Severity.SAFE
        )

    def test_not_null_with_default_is_safe(self):
        assert (
            severity_of(
                "ALTER TABLE `remittance_attempt` ADD COLUMN `installments` "
                "smallint UNSIGNED NOT NULL DEFAULT 1"
            )
            is Severity.SAFE
        )

    def test_not_null_without_default_is_controlled(self):
        finding = classify_statement(
            "ALTER TABLE `remittance_attempt` ADD COLUMN `installments` smallint UNSIGNED NOT NULL"
        )
        assert finding.severity is Severity.CONTROLLED
        assert "DEFAULT" in finding.reason

    def test_postgres_not_null_without_default_is_controlled(self):
        assert (
            severity_of('ALTER TABLE "schedule" ADD "suspended" boolean NOT NULL')
            is Severity.CONTROLLED
        )

    def test_postgres_not_null_with_default_is_safe(self):
        assert (
            severity_of('ALTER TABLE "schedule" ADD "suspended" boolean NOT NULL DEFAULT false')
            is Severity.SAFE
        )

    def test_a_default_in_a_sibling_clause_does_not_rescue_the_new_column(self):
        finding = classify_statement(
            "ALTER TABLE `t` ADD COLUMN `a` varchar(10) NOT NULL, "
            "ALTER COLUMN `b` SET DEFAULT 'x'"
        )
        assert finding.severity is Severity.CONTROLLED

    def test_lock_none_is_not_read_as_the_new_column_being_not_null(self):
        assert (
            severity_of(
                "ALTER TABLE `t` ALGORITHM=INPLACE, LOCK=NONE, ADD COLUMN `a` varchar(10) NULL"
            )
            is Severity.SAFE
        )


# ---------------------------------------------------------------------------
# Aceite: NOT NULL nos dois dialetos é breaking
# ---------------------------------------------------------------------------


class TestNotNullIsBreakingInBothDialects:
    def test_mysql_modify_not_null(self):
        # consumidor Django, ledger/0042 — o caso do PR da Evidência 1 da ADR
        finding = classify_statement(
            "ALTER TABLE ledger_supplierservice MODIFY external_id BIGINT NOT NULL, "
            "ALGORITHM=INPLACE, LOCK=NONE"
        )
        assert finding.severity is Severity.BREAKING
        assert "NOT NULL" in finding.operation

    def test_postgres_alter_column_set_not_null(self):
        finding = classify_statement(
            'ALTER TABLE "schedule" ALTER COLUMN "phone" SET NOT NULL'
        )
        assert finding.severity is Severity.BREAKING
        assert "NOT NULL" in finding.operation

    def test_postgres_drop_not_null_is_only_controlled(self):
        # tornar o campo nullable afrouxa o schema, não quebra leitor
        assert (
            severity_of('ALTER TABLE "schedule" ALTER COLUMN "phone" DROP NOT NULL')
            is Severity.CONTROLLED
        )

    def test_mysql_modify_without_not_null_is_still_a_type_change(self):
        assert (
            severity_of("ALTER TABLE plans_plan MODIFY ip_address VARCHAR(39)")
            is Severity.BREAKING
        )

    def test_lock_none_is_not_mistaken_for_not_null(self):
        finding = classify_statement(
            "ALTER TABLE `remittance_voucher` ALGORITHM=INPLACE, LOCK=NONE, "
            "ADD INDEX `ix_origin_channel` (`origin_channel`)"
        )
        assert finding.severity is Severity.SAFE


# ---------------------------------------------------------------------------
# Aceite: ADD CONSTRAINT ... UNIQUE é breaking, demais constraints controlled
# ---------------------------------------------------------------------------


class TestAddConstraint:
    def test_unique_constraint_is_breaking(self):
        finding = classify_statement(
            'ALTER TABLE "meeting_recording" ADD CONSTRAINT "UQ_0edf786" '
            'UNIQUE ("scheduleMeetingId")'
        )
        assert finding.severity is Severity.BREAKING
        assert "UNIQUE" in finding.operation

    def test_foreign_key_constraint_is_controlled(self):
        assert (
            severity_of(
                'ALTER TABLE "message_tracking" ADD CONSTRAINT "FK_1801c38" '
                'FOREIGN KEY ("meetingIdMeetingId") REFERENCES "schedule"("meetingId") '
                "ON DELETE NO ACTION ON UPDATE NO ACTION"
            )
            is Severity.CONTROLLED
        )

    def test_primary_key_constraint_is_controlled(self):
        assert (
            severity_of(
                'ALTER TABLE "schedule" ADD CONSTRAINT "PK_920854" PRIMARY KEY ("meetingId")'
            )
            is Severity.CONTROLLED
        )

    def test_check_constraint_is_controlled(self):
        assert (
            severity_of("ALTER TABLE orders ADD CONSTRAINT chk_qty CHECK (qty > 0)")
            is Severity.CONTROLLED
        )

    def test_constraint_named_unique_is_not_mistaken_for_a_unique_constraint(self):
        assert (
            severity_of(
                "ALTER TABLE `treasury_charge` ADD CONSTRAINT `unique_bill_charge_method` "
                "FOREIGN KEY (`charge_method_id`) REFERENCES `treasury_paymentmethod` (`id`) "
                "ON DELETE SET NULL ON UPDATE RESTRICT"
            )
            is Severity.CONTROLLED
        )

    def test_drop_constraint_is_controlled(self):
        assert (
            severity_of(
                'ALTER TABLE "meeting_room" DROP CONSTRAINT "FK_bebe8b25"'
            )
            is Severity.CONTROLLED
        )


# ---------------------------------------------------------------------------
# Aceite: ALTER TYPE ... ADD VALUE é safe, outros ALTER TYPE são unknown
# ---------------------------------------------------------------------------


class TestAlterType:
    def test_add_value_is_safe(self):
        finding = classify_statement(
            "ALTER TYPE \"schedule_status_enum\" ADD VALUE 'cancelled'"
        )
        assert finding.severity is Severity.SAFE
        assert "ADD VALUE" in finding.operation

    def test_add_value_if_not_exists_is_safe(self):
        assert (
            severity_of("ALTER TYPE status_enum ADD VALUE IF NOT EXISTS 'cancelled'")
            is Severity.SAFE
        )

    def test_rename_to_is_unknown_not_a_guess(self):
        # 1682010569216-AutoMigrate.ts — o único `unknown` do corpus TypeORM
        finding = classify_statement(
            'ALTER TYPE "public"."schedule_status_enum" RENAME TO "schedule_status_enum_old"'
        )
        assert finding.severity is Severity.UNKNOWN
        assert finding.operation == "ALTER TYPE"

    def test_rename_value_is_unknown(self):
        assert (
            severity_of("ALTER TYPE status_enum RENAME VALUE 'a' TO 'b'")
            is Severity.UNKNOWN
        )


# ---------------------------------------------------------------------------
# Aceite: SET, COMMENT ON e comentários não geram severidade
# ---------------------------------------------------------------------------


class TestStatementsWithoutSeverity:
    @pytest.mark.parametrize(
        "sql",
        [
            "SET @orig_sql_mode = @@SESSION.sql_mode",
            "SET SESSION lock_wait_timeout = 60",
            "SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), "
            "'STRICT_TRANS_TABLES')",
            "SET @orig_lock_wait_timeout = @@SESSION.lock_wait_timeout",
        ],
    )
    def test_set_has_no_severity(self, sql):
        finding = classify_statement(sql)
        assert finding.severity is Severity.NONE
        assert finding.operation == "SET"

    def test_comment_on_has_no_severity(self):
        finding = classify_statement(
            "COMMENT ON COLUMN schedule.status IS 'situação da consulta'"
        )
        assert finding.severity is Severity.NONE
        assert finding.operation == "COMMENT ON"

    def test_line_comment_has_no_severity(self):
        assert severity_of("-- legacy indexes") is Severity.NONE

    def test_block_comment_has_no_severity(self):
        assert severity_of("/* nada a fazer aqui */") is Severity.NONE

    def test_empty_statement_has_no_severity(self):
        assert severity_of("   ") is Severity.NONE

    @pytest.mark.parametrize("sql", ["BEGIN", "COMMIT", "START TRANSACTION", "ROLLBACK"])
    def test_transaction_control_has_no_severity(self, sql):
        assert severity_of(sql) is Severity.NONE

    @pytest.mark.parametrize("sql", ["BEGIN", "COMMIT", "START TRANSACTION", "ROLLBACK"])
    def test_transaction_control_reports_its_own_verb(self, sql):
        # Não um rótulo genérico: `operation` diz o que o statement diz.
        assert classify_statement(sql).operation == sql

    def test_a_comment_does_not_hide_the_ddl_that_follows_it(self):
        assert (
            severity_of("-- remove a coluna\nALTER TABLE t DROP COLUMN c")
            is Severity.BREAKING
        )


# ---------------------------------------------------------------------------
# Aceite: verbo não reconhecido devolve unknown, nunca um palpite
# ---------------------------------------------------------------------------


class TestUnrecognizedVerbsReturnUnknown:
    @pytest.mark.parametrize(
        "sql",
        [
            "PREPARE stmt FROM @query",
            "EXECUTE stmt",
            "DEALLOCATE PREPARE stmt",
            "INSERT INTO `claim_version`(version) VALUES ('3.01.00')",
            "UPDATE bookings_booking SET status = 1 WHERE clinic_id = 2",
            "DELETE FROM bookings_booking WHERE id = 1",
            "TRUNCATE TABLE bookings_booking",
            "CREATE MATERIALIZED VIEW mv AS SELECT 1",
            "GRANT SELECT ON schedule TO analytics",
            "CALL some_procedure()",
            "REFRESH MATERIALIZED VIEW mv",
            "VACUUM FULL schedule",
        ],
    )
    def test_verb_outside_the_table_is_unknown(self, sql):
        assert severity_of(sql) is Severity.UNKNOWN

    def test_unknown_finding_names_the_statement_it_could_not_read(self):
        finding = classify_statement("REFRESH MATERIALIZED VIEW mv")
        assert finding.severity is Severity.UNKNOWN
        assert "REFRESH" in finding.operation
        assert finding.reason

    def test_alter_table_with_an_unrecognized_action_is_unknown(self):
        finding = classify_statement("ALTER TABLE orders DISABLE TRIGGER ALL")
        assert finding.severity is Severity.UNKNOWN
        assert finding.operation == "ALTER TABLE"

    def test_alter_column_with_an_unrecognized_action_is_unknown(self):
        assert (
            severity_of("ALTER TABLE orders ALTER COLUMN qty SET STATISTICS 100")
            is Severity.UNKNOWN
        )

    def test_gibberish_is_unknown(self):
        assert severity_of("isso não é sql") is Severity.UNKNOWN

    def test_dynamic_sql_hidden_in_a_variable_never_becomes_a_severity(self):
        # journal/0012_remove_unused_indexes.py monta o DDL numa variável;
        # o SET não pode virar `controlled` só porque a string contém DROP INDEX
        finding = classify_statement(
            "SET @query = (SELECT IF (EXISTS (SELECT index_name FROM information_schema.statistics "
            "WHERE table_name = 'journal_entryblock'), "
            "'DROP INDEX journal_entryblock_ac6f2659 ON journal_entryblock', 'SELECT 1'))"
        )
        assert finding.severity is Severity.NONE
        assert finding.operation == "SET"


# ---------------------------------------------------------------------------
# Aceite: mapeamento explícito para cada verbo do corpus, nos dois dialetos
# ---------------------------------------------------------------------------


class TestVerbMappingPostgres:
    @pytest.mark.parametrize(
        "sql, expected",
        [
            (
                'ALTER TABLE "meeting_room" ADD "type" "meeting_room_type_enum" '
                "NOT NULL DEFAULT 'peer-to-peer'",
                Severity.SAFE,
            ),
            (
                'ALTER TABLE "schedule" ADD "suspended" boolean NOT NULL DEFAULT false',
                Severity.SAFE,
            ),
            (
                'ALTER TABLE "meeting_recording" ADD CONSTRAINT "UQ_0ed" UNIQUE ("id")',
                Severity.BREAKING,
            ),
            (
                'ALTER TABLE "meeting_room" DROP CONSTRAINT "FK_bebe"',
                Severity.CONTROLLED,
            ),
            (
                'CREATE TABLE "message_tracking" ("id" uuid NOT NULL, '
                'CONSTRAINT "PK_d28" PRIMARY KEY ("id"))',
                Severity.SAFE,
            ),
            ('ALTER TABLE "schedule" DROP COLUMN "status"', Severity.BREAKING),
            (
                'ALTER TABLE "schedule" ALTER COLUMN "status" DROP DEFAULT',
                Severity.CONTROLLED,
            ),
            (
                'ALTER TABLE "schedule" ALTER COLUMN "status" SET DEFAULT \'scheduled\'',
                Severity.CONTROLLED,
            ),
            (
                'ALTER TABLE "schedule" ALTER COLUMN "status" TYPE "schedule_status_enum" '
                'USING "status"::"text"::"schedule_status_enum"',
                Severity.BREAKING,
            ),
            (
                "CREATE TYPE \"schedule_status_enum\" AS ENUM('scheduled', 'sent')",
                Severity.SAFE,
            ),
            (
                'CREATE INDEX "IDX_9b8" ON "meeting_log" ("scheduleMeetingId")',
                Severity.SAFE,
            ),
            ('DROP INDEX "IDX_dec024fe"', Severity.CONTROLLED),
            (
                'ALTER TABLE "schedule" RENAME COLUMN "isClosed" TO "status"',
                Severity.BREAKING,
            ),
            (
                'CREATE UNIQUE INDEX "IDX_dec" ON "message_tracking" ("event", "messageId")',
                Severity.BREAKING,
            ),
            (
                'ALTER TYPE "public"."schedule_status_enum" RENAME TO "old"',
                Severity.UNKNOWN,
            ),
            ('DROP TYPE "schedule_status_enum"', Severity.CONTROLLED),
            (
                'CREATE SEQUENCE "schedule_meetingId_seq" OWNED BY "schedule"."meetingId"',
                Severity.SAFE,
            ),
            (
                'ALTER TABLE "meeting_participant_joined" RENAME TO "meeting_room"',
                Severity.BREAKING,
            ),
        ],
    )
    def test_postgres_verb(self, sql, expected):
        assert severity_of(sql) is expected


class TestVerbMappingMysql:
    @pytest.mark.parametrize(
        "sql, expected",
        [
            (
                "ALTER TABLE `remittance_attempt` ALGORITHM=INPLACE, LOCK=NONE, "
                "ADD COLUMN `installments` smallint UNSIGNED NOT NULL DEFAULT 1",
                Severity.SAFE,
            ),
            (
                "ALTER TABLE `treasury_charge` ADD CONSTRAINT `fk_treasury_charge_method` "
                "FOREIGN KEY (`charge_method_id`) REFERENCES `treasury_paymentmethod` (`id`)",
                Severity.CONTROLLED,
            ),
            (
                "ALTER TABLE `journal_block` DROP CONSTRAINT `journal_block_updated_by_id_fk`",
                Severity.CONTROLLED,
            ),
            (
                "CREATE TABLE `claim_version` (`version` varchar(8) NOT NULL PRIMARY KEY, "
                "`is_default` bool NOT NULL)",
                Severity.SAFE,
            ),
            ("ALTER TABLE `bookings_booking` DROP COLUMN `status`", Severity.BREAKING),
            (
                "ALTER TABLE `directory_entry` ALTER COLUMN `_updated_at` DROP DEFAULT",
                Severity.CONTROLLED,
            ),
            (
                "ALTER TABLE ledger_supplier MODIFY external_id BIGINT NOT NULL, "
                "ALGORITHM=INPLACE, LOCK=NONE",
                Severity.BREAKING,
            ),
            (
                "CREATE INDEX subjects_subject_subject_code_clinic_id ON `subjects_subject` "
                "(subject_code, clinic_id)",
                Severity.SAFE,
            ),
            (
                "ALTER TABLE `treasury_charge` ALGORITHM=INPLACE, LOCK=NONE, "
                "ADD INDEX `ix_treasury_charge_method_id` (`charge_method_id`)",
                Severity.SAFE,
            ),
            (
                "ALTER TABLE `subjects_subjectsupplier` ALGORITHM=INPLACE, LOCK=NONE, "
                "ADD UNIQUE INDEX `unique_subject_supplier_tenant` (`subject_id`, `clinic_id`)",
                Severity.BREAKING,
            ),
            ("DROP INDEX journal_entryblock_ac6f2659 ON journal_entryblock", Severity.CONTROLLED),
            (
                "ALTER TABLE `claim_lot` CHANGE `version` `version_id` varchar(8) NOT NULL",
                Severity.BREAKING,
            ),
            ("ALTER TABLE orders RENAME TO orders_old", Severity.BREAKING),
        ],
    )
    def test_mysql_verb(self, sql, expected):
        assert severity_of(sql) is expected


# ---------------------------------------------------------------------------
# ALTER TABLE com várias ações na mesma sentença
# ---------------------------------------------------------------------------


class TestMultiActionAlterTable:
    def test_takes_the_highest_severity_of_the_actions(self):
        # subjects/0053 — adiciona coluna (safe) e índice único (breaking)
        finding = classify_statement(
            "ALTER TABLE `subjects_subject` ALGORITHM=INPLACE, LOCK=NONE, "
            "ADD COLUMN `unique_ref_code` varchar(47) NULL, "
            "ADD UNIQUE INDEX `unique_subject_unique_ref_code` (`unique_ref_code`)"
        )
        assert finding.severity is Severity.BREAKING

    def test_add_column_plus_plain_index_stays_safe(self):
        finding = classify_statement(
            "ALTER TABLE `remittance_reversalattempt` ALGORITHM=INPLACE, LOCK=NONE, "
            "ADD COLUMN `origin` varchar(32) NOT NULL DEFAULT 'manual', "
            "ADD INDEX `remittance_reversalattempt_origin_53f467aa` (`origin`)"
        )
        assert finding.severity is Severity.SAFE

    def test_a_comma_inside_an_index_column_list_is_not_an_action_boundary(self):
        finding = classify_statement(
            "ALTER TABLE `treasury_charge` ALGORITHM=INPLACE, LOCK=NONE, "
            "ADD INDEX `ix_treasury_charge_tenant_kind_dates` "
            "(`clinic_id`, `_deleted`, `kind`, `settle_date`, `date_added`, `id`)"
        )
        assert finding.severity is Severity.SAFE

    def test_a_default_string_is_not_read_as_a_verb(self):
        finding = classify_statement(
            "ALTER TABLE `remittance_voucher` ALGORITHM=INPLACE, LOCK=NONE, "
            "ADD COLUMN `origin_channel` VARCHAR(10) NOT NULL DEFAULT 'drop column' "
            "COMMENT 'Origem da geração'"
        )
        assert finding.severity is Severity.SAFE


# ---------------------------------------------------------------------------
# A tabela de ações e a tabela de regras não podem sair de sincronia
# ---------------------------------------------------------------------------


class TestActionTableCoverage:
    def test_every_action_group_has_a_rule(self):
        """Grupo novo na regex sem regra correspondente vira `unknown`, não crash.

        Este teste é o lugar onde isso tem que doer — não numa action rodando
        em PR. Falha aqui quer dizer: você adicionou um grupo e esqueceu a regra.
        """
        kinds = set(_ALTER_TABLE_ACTIONS.groupindex) - {"noise"}
        assert kinds, "a regex de ações perdeu todos os grupos"
        for kind in sorted(kinds):
            finding = _classify_alter_table_clause(kind, "ADD COLUMN", "t", "c", "ADD COLUMN c int")
            assert "sem regra no classificador" not in finding.reason, kind

    def test_the_action_regex_and_the_rules_table_have_the_same_keys(self):
        assert set(_ALTER_TABLE_ACTIONS.groupindex) - {"noise"} == set(_ALTER_TABLE_RULES)

    def test_every_refiner_belongs_to_a_rule(self):
        # Refinador sob chave digitada errada é código morto silencioso: a ação
        # continua classificando, só que sempre pela regra não refinada.
        assert set(_ALTER_TABLE_REFINERS) <= set(_ALTER_TABLE_RULES)

    def test_an_unknown_group_degrades_instead_of_raising(self):
        finding = _classify_alter_table_clause("grupo_novo", "ADD FOO", "t", "c", "ADD FOO")
        assert finding.severity is Severity.UNKNOWN
        assert "sem regra no classificador" in finding.reason

    @pytest.mark.parametrize(
        "sql, expected",
        [
            ("ALTER TABLE t ADD COLUMN c int", "ADD COLUMN"),
            ('ALTER TABLE t ADD "c" int', "ADD COLUMN"),
            ("ALTER TABLE t ADD COLUMN IF NOT EXISTS c int", "ADD COLUMN"),
            ("ALTER TABLE t DROP COLUMN c", "DROP COLUMN"),
            ("ALTER TABLE t DROP c", "DROP COLUMN"),
            ("ALTER TABLE t DROP COLUMN IF EXISTS c", "DROP COLUMN"),
        ],
    )
    def test_column_actions_use_one_stable_label(self, sql, expected):
        # A QQ-2158 agrupa por `operation`. A forma dominante no PostgreSQL é
        # `ADD "c" int`, sem `COLUMN`, e a sintaxe opcional não pode partir a
        # mesma operação em vários rótulos.
        assert classify_statement(sql).operation == expected

    def test_operation_never_claims_an_index_for_a_key_drop(self):
        for sql, expected in [
            ("ALTER TABLE t DROP PRIMARY KEY", "DROP PRIMARY KEY"),
            ("ALTER TABLE t DROP FOREIGN KEY fk_a", "DROP FOREIGN KEY"),
            ("ALTER TABLE t ADD PRIMARY KEY (id)", "ADD PRIMARY KEY"),
        ]:
            assert classify_statement(sql).operation == expected


# ---------------------------------------------------------------------------
# Invariantes das tabelas de regras
# ---------------------------------------------------------------------------
#
# Trazidas de `tests/test_django.py`, onde pegaram três razões que o Slack
# mostra começando em minúscula. As tabelas daqui são o artefato que a QQ-2162
# entrega ao time de dados para editar, e estas asserções são as que reclamam
# do que um editor de tabela erra de verdade: uma frase que não é frase, um
# `{placeholder}` que a regra não sabe preencher.


def placeholders(template):
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


# Cláusula que dispara cada refinamento, para exercitar a razão que ele
# devolve — nenhuma delas está na tabela. A asserção de cobertura ao lado
# impede refinador novo entrar sem a sua.
ALTER_TABLE_REFINER_TRIGGERS = {
    "add_constraint": "ADD CONSTRAINT uq UNIQUE (a)",
    "add_column": "ADD COLUMN c int NOT NULL",
    "modify": "MODIFY c int NOT NULL",
    "alter_column": "ALTER COLUMN c SET NOT NULL",
}

STATEMENT_REFINER_TRIGGERS = {
    "_refine_create_index": "CREATE UNIQUE INDEX ix ON t (a)",
    "_refine_alter_type": "ALTER TYPE e ADD VALUE 'x'",
}


def every_rule():
    """Toda regra das três tabelas, com um rótulo para a mensagem de falha."""
    for kind, rule in _ALTER_TABLE_RULES.items():
        yield f"_ALTER_TABLE_RULES[{kind}]", rule
    for subaction, _, rule in _ALTER_COLUMN_RULES:
        yield f"_ALTER_COLUMN_RULES[{subaction}]", rule
    for statement in _STATEMENT_RULES:
        yield f"_STATEMENT_RULES[{statement.head[:24]}]", statement.rule


class TestRuleTableInvariants:
    @pytest.mark.parametrize("label, rule", list(every_rule()), ids=lambda v: getattr(v, "reason", v)[:40])
    def test_every_reason_template_is_a_sentence(self, label, rule):
        assert rule.reason[0].isupper(), label
        assert rule.reason.endswith("."), label

    @pytest.mark.parametrize("kind", sorted(_ALTER_TABLE_RULES))
    def test_alter_table_reasons_only_cite_the_table_and_the_column(self, kind):
        # `_classify_alter_table_clause` só sabe preencher estes dois; qualquer
        # outro nome no template estoura em produção, não aqui.
        assert placeholders(_ALTER_TABLE_RULES[kind].reason) <= {"table", "column"}, kind

    @pytest.mark.parametrize("subaction", [sub for sub, _, _ in _ALTER_COLUMN_RULES])
    def test_alter_column_reasons_only_cite_the_table_and_the_column(self, subaction):
        rule = next(r for sub, _, r in _ALTER_COLUMN_RULES if sub == subaction)
        assert placeholders(rule.reason) <= {"table", "column"}, subaction

    @pytest.mark.parametrize("statement", _STATEMENT_RULES, ids=lambda st: st.head[:24])
    def test_statement_reasons_only_cite_the_object_name(self, statement):
        assert placeholders(statement.rule.reason) <= {"name"}, statement.head

    @pytest.mark.parametrize("kind", sorted(ALTER_TABLE_REFINER_TRIGGERS))
    def test_every_alter_table_refiner_returns_a_sentence(self, kind):
        rule = _ALTER_TABLE_REFINERS[kind](ALTER_TABLE_REFINER_TRIGGERS[kind])
        assert rule is not None, f"a cláusula de {kind} deixou de disparar o refinamento"
        assert rule.reason[0].isupper(), kind
        assert rule.reason.endswith("."), kind
        assert placeholders(rule.reason) <= {"table", "column"}, kind

    def test_every_alter_table_refiner_has_a_trigger(self):
        assert set(_ALTER_TABLE_REFINERS) == set(ALTER_TABLE_REFINER_TRIGGERS)

    @pytest.mark.parametrize("name", sorted(STATEMENT_REFINER_TRIGGERS))
    def test_every_statement_refiner_returns_a_sentence(self, name):
        refine = next(
            st.refine for st in _STATEMENT_RULES if st.refine and st.refine.__name__ == name
        )
        rule = refine(STATEMENT_REFINER_TRIGGERS[name])
        assert rule is not None, f"o statement de {name} deixou de disparar o refinamento"
        assert rule.reason[0].isupper(), name
        assert rule.reason.endswith("."), name
        assert placeholders(rule.reason) <= {"name"}, name

    def test_every_statement_refiner_has_a_trigger(self):
        declared = {st.refine.__name__ for st in _STATEMENT_RULES if st.refine}
        assert declared == set(STATEMENT_REFINER_TRIGGERS)


# ---------------------------------------------------------------------------
# Identificador citado colado no verbo, sem espaço
# ---------------------------------------------------------------------------


class TestNoSpaceBeforeQuotedIdentifier:
    """`DROP TABLE"orders"` é PostgreSQL válido: aspas não precisam de espaço.

    Todo statement do corpus e todo teste escrito à mão põe o espaço, então
    esta é a classe de falha que a suíte não enxergava. Reconhecer o verbo não
    pode depender de haver espaço depois dele.
    """

    @pytest.mark.parametrize(
        "sql, expected, operation",
        [
            ('DROP TABLE"orders"', Severity.BREAKING, "DROP TABLE"),
            ('CREATE TABLE"t"("id" int)', Severity.SAFE, "CREATE TABLE"),
            ('DROP TYPE"e"', Severity.CONTROLLED, "DROP TYPE"),
            ('DROP INDEX"ix"', Severity.CONTROLLED, "DROP INDEX"),
            ("CREATE TYPE\"e\" AS ENUM('a')", Severity.SAFE, "CREATE TYPE"),
            ('CREATE SEQUENCE"s"', Severity.SAFE, "CREATE SEQUENCE"),
            ("ALTER TYPE\"e\" ADD VALUE 'x'", Severity.SAFE, "ALTER TYPE ... ADD VALUE"),
            ('ALTER TABLE"t" DROP COLUMN"c"', Severity.BREAKING, "DROP COLUMN"),
            ('CREATE INDEX"ix" ON"t" ("a")', Severity.SAFE, "CREATE INDEX"),
            ('ALTER TABLE"t" ADD"c" int', Severity.SAFE, "ADD COLUMN"),
        ],
    )
    def test_verb_is_recognized_without_a_separating_space(self, sql, expected, operation):
        finding = classify_statement(sql)
        assert finding.severity is expected, sql
        assert finding.operation == operation, sql

    @pytest.mark.parametrize(
        "sql, name",
        [
            ('DROP TABLE"orders"', "orders"),
            ('CREATE INDEX"ix" ON"t" ("a")', "t"),
            ('ALTER TABLE"t" DROP COLUMN"c"', "t"),
            ('DROP INDEX CONCURRENTLY"ix"', "ix"),
        ],
    )
    def test_the_object_name_is_still_read(self, sql, name):
        assert f"`{name}`" in classify_statement(sql).reason

    def test_optional_syntax_still_does_not_become_the_name(self):
        finding = classify_statement('DROP TABLE IF EXISTS"orders"')
        assert "`orders`" in finding.reason
        assert finding.operation == "DROP TABLE"


# ---------------------------------------------------------------------------
# `operation` é um rótulo estável, não a sintaxe que o autor escolheu
# ---------------------------------------------------------------------------


class TestOperationIsStable:
    @pytest.mark.parametrize(
        "sql, expected",
        [
            ("DROP TABLE orders", "DROP TABLE"),
            ("DROP TABLE IF EXISTS orders", "DROP TABLE"),
            ("CREATE TABLE t (id int)", "CREATE TABLE"),
            ("CREATE TABLE IF NOT EXISTS t (id int)", "CREATE TABLE"),
            ("CREATE TEMPORARY TABLE t (id int)", "CREATE TEMPORARY TABLE"),
            ("DROP INDEX ix", "DROP INDEX"),
            ("DROP INDEX CONCURRENTLY IF EXISTS ix", "DROP INDEX"),
            ("CREATE INDEX ix ON t (a)", "CREATE INDEX"),
            ("CREATE INDEX CONCURRENTLY ix ON t (a)", "CREATE INDEX"),
            ("CREATE UNIQUE INDEX ix ON t (a)", "CREATE UNIQUE INDEX"),
            ("CREATE UNIQUE INDEX CONCURRENTLY ix ON t (a)", "CREATE UNIQUE INDEX"),
            ("DROP TYPE IF EXISTS e", "DROP TYPE"),
            ("CREATE SEQUENCE IF NOT EXISTS s", "CREATE SEQUENCE"),
        ],
    )
    def test_optional_syntax_does_not_split_one_operation_into_many_labels(self, sql, expected):
        assert classify_statement(sql).operation == expected

    def test_a_key_action_still_says_which_key_it_was(self):
        # Onde o cabeçalho é informação, e não sintaxe, derivar continua certo.
        assert classify_statement("ALTER TABLE t ADD PRIMARY KEY (id)").operation == (
            "ADD PRIMARY KEY"
        )
        assert classify_statement("ALTER TABLE t ADD INDEX ix (a)").operation == "ADD INDEX"


# ---------------------------------------------------------------------------
# Nenhum valor de literal pode chegar ao Slack
# ---------------------------------------------------------------------------


class TestReasonsNeverLeakLiterals:
    def test_unrecognized_insert_does_not_echo_subject_data(self):
        finding = classify_statement(
            "INSERT INTO subjects (cpf, nome) VALUES ('12345678900', 'Maria Silva')"
        )
        assert finding.severity is Severity.UNKNOWN
        assert "12345678900" not in finding.reason
        assert "Maria Silva" not in finding.reason
        assert "INSERT INTO subjects" in finding.reason

    def test_unrecognized_update_does_not_echo_values(self):
        finding = classify_statement("UPDATE subjects SET nome = 'Maria Silva' WHERE id = 1")
        assert "Maria Silva" not in finding.reason

    def test_an_unquoted_identifier_is_not_echoed(self):
        # Sem aspa nenhuma, e é dado de paciente do mesmo jeito. O corte por
        # aspas não pegava este, e é ele que chega aqui pelo `queryRunner.query()`
        # do TypeORM e pelo `op.execute()` do Alembic.
        finding = classify_statement("CALL migrate_subject(12345678900)")
        assert finding.severity is Severity.UNKNOWN
        assert "12345678900" not in finding.reason
        assert "CALL migrate_subject" in finding.reason

    @pytest.mark.parametrize(
        "sql",
        [
            "ALTER TABLE t ADD COLUMN c varchar(255) NOT NULL",
            "ALTER TABLE t ADD COLUMN c NUMERIC(10,2)",
        ],
    )
    def test_a_number_that_is_part_of_a_type_is_not_a_value(self, sql):
        # O corte por dígito não pode engolir o statement inteiro: `varchar(255)`
        # é sintaxe, não dado, e é o que o corpus escreve.
        assert classify_statement(sql).severity is not Severity.UNKNOWN

    def test_a_short_number_survives_the_echo(self):
        assert "t1" in classify_statement("GRANT SELECT ON t1 TO leitor").reason

    def test_no_reason_in_the_corpus_shapes_contains_a_quote_character(self):
        statements = [
            "INSERT INTO t (a) VALUES ('segredo')",
            "CALL faz_coisa('segredo')",
            "ALTER TABLE t ADD COLUMN c varchar(10) NOT NULL DEFAULT 'segredo'",
            "SET SESSION sql_mode = 'segredo'",
        ]
        for sql in statements:
            assert "segredo" not in classify_statement(sql).reason, sql


# ---------------------------------------------------------------------------
# Cabeçalhos opcionais que a regex tem que atravessar
# ---------------------------------------------------------------------------


class TestOptionalClauses:
    def test_add_column_if_not_exists_names_the_column_not_the_keyword(self):
        finding = classify_statement("ALTER TABLE t ADD COLUMN IF NOT EXISTS c int")
        assert "`c`" in finding.reason
        assert "IF" not in finding.reason

    def test_drop_column_if_exists_names_the_column(self):
        finding = classify_statement("ALTER TABLE t DROP COLUMN IF EXISTS c")
        assert finding.severity is Severity.BREAKING
        assert "`c`" in finding.reason

    def test_create_index_on_only_names_the_table_not_only(self):
        finding = classify_statement("CREATE INDEX idx ON ONLY t (a)")
        assert "`t`" in finding.reason
        assert "ONLY" not in finding.reason

    def test_alter_table_only_names_the_table(self):
        finding = classify_statement("ALTER TABLE ONLY t DROP COLUMN c")
        assert "`t`" in finding.reason

    @pytest.mark.parametrize("subaction", ["DROP IDENTITY", "DROP EXPRESSION"])
    def test_alter_column_drop_subaction_is_not_read_as_a_column_drop(self, subaction):
        finding = classify_statement(f"ALTER TABLE t ALTER COLUMN c {subaction}")
        assert finding.severity is Severity.UNKNOWN
        assert "DROP COLUMN" not in finding.operation


# ---------------------------------------------------------------------------
# Forma do Finding
# ---------------------------------------------------------------------------


class TestFindingShape:
    def test_reason_is_a_non_empty_portuguese_sentence(self):
        finding = classify_statement('ALTER TABLE "schedule" DROP COLUMN "status"')
        assert finding.reason
        assert finding.reason[0].isupper()
        assert finding.reason.endswith(".")

    def test_reason_names_the_table_and_the_column(self):
        finding = classify_statement('ALTER TABLE "schedule" DROP COLUMN "status"')
        assert "schedule" in finding.reason
        assert "status" in finding.reason

    def test_reason_names_the_table_for_a_mysql_statement(self):
        finding = classify_statement("DROP TABLE `claim_version`")
        assert "claim_version" in finding.reason

    def test_operation_is_the_verb(self):
        assert (
            classify_statement('ALTER TABLE "schedule" DROP COLUMN "status"').operation
            == "DROP COLUMN"
        )

    def test_every_finding_has_operation_and_reason(self):
        statements = [
            'ALTER TABLE "schedule" DROP COLUMN "status"',
            "SET SESSION lock_wait_timeout = 60",
            "REFRESH MATERIALIZED VIEW mv",
            'CREATE TABLE "t" ("id" uuid NOT NULL)',
            "-- só um comentário",
            "   ",
        ]
        for sql in statements:
            finding = classify_statement(sql)
            assert finding.operation, sql
            assert finding.reason, sql

    def test_leading_and_trailing_noise_is_tolerated(self):
        assert (
            severity_of('\n\n  ALTER TABLE "schedule" DROP COLUMN "status" ;  \n')
            is Severity.BREAKING
        )

    def test_lowercase_sql_is_classified_the_same(self):
        assert (
            severity_of('alter table "schedule" drop column "status"')
            is Severity.BREAKING
        )


# ---------------------------------------------------------------------------
# looks_like_ddl
# ---------------------------------------------------------------------------


class TestLooksLikeDDL:
    """O portão que decide se uma string solta vale uma passada pelo `sql.py`.

    Existe para o corpo do `RunPython` do Django, onde qualquer string do
    arquivo chega junto. Sem ele, uma mensagem de erro viraria `unknown` e todo
    `RunPython` com texto no corpo pediria revisão manual; com ele frouxo
    demais, um `ALTER TABLE` real passaria batido.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "ALTER TABLE app_thing DROP COLUMN legacy_code",
            "alter table app_thing drop column legacy_code",
            "  \n CREATE INDEX idx_thing ON app_thing (title)",
            "DROP TABLE app_thing",
            "RENAME TABLE a TO b",
            "TRUNCATE TABLE app_thing",
            "COMMENT ON TABLE app_thing IS 'x'",
        ],
    )
    def test_statements_that_change_or_describe_schema(self, sql):
        assert looks_like_ddl(sql) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "sem titulo",
            "DROP everything is not what this does",
            "Erro ao processar o registro",
            "SELECT id FROM app_thing",
            "UPDATE app_thing SET title = 'x'",
            "app_thing",
            "created_at",
        ],
    )
    def test_text_that_is_not_a_ddl_statement(self, text):
        assert looks_like_ddl(text) is False

    def test_a_leading_comment_does_not_hide_the_verb(self):
        assert looks_like_ddl("-- ajusta a tabela\nALTER TABLE app_thing DROP COLUMN c") is True
