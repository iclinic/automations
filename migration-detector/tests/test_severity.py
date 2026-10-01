import dataclasses
import json

import pytest

from detect.severity import (
    SEVERITY_META,
    Finding,
    UnknownSeverity,
    Presentation,
    Severity,
    max_severity,
    presentation,
    severity_rank,
    worst,
)

# ---------------------------------------------------------------------------
# Vocabulário
# ---------------------------------------------------------------------------


class TestSeverityVocabulary:
    def test_has_exactly_the_five_expected_members(self):
        assert [s.value for s in Severity] == [
            "none",
            "safe",
            "controlled",
            "unknown",
            "breaking",
        ]

    def test_is_a_str_enum_so_it_serializes_as_plain_text(self):
        assert Severity.BREAKING == "breaking"
        assert json.dumps({"severity": Severity.SAFE}) == '{"severity": "safe"}'

    def test_string_formatting_does_not_give_the_plain_value(self):
        # Armadilha para quem for montar a mensagem do Slack: interpolar o
        # membro escreve "Severity.SAFE". O valor vem de `.value`.
        assert f"{Severity.SAFE}" != "safe"
        assert Severity.SAFE.value == "safe"

    def test_can_be_rebuilt_from_its_value(self):
        assert Severity("controlled") is Severity.CONTROLLED

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            Severity("catastrophic")


# ---------------------------------------------------------------------------
# Ordenação
# ---------------------------------------------------------------------------


class TestSeverityRank:
    def test_orders_none_below_safe_below_controlled(self):
        assert severity_rank(Severity.NONE) < severity_rank(Severity.SAFE)
        assert severity_rank(Severity.SAFE) < severity_rank(Severity.CONTROLLED)

    def test_unknown_outranks_controlled(self):
        assert severity_rank(Severity.UNKNOWN) > severity_rank(Severity.CONTROLLED)

    def test_breaking_outranks_unknown(self):
        assert severity_rank(Severity.BREAKING) > severity_rank(Severity.UNKNOWN)

    def test_every_member_has_a_distinct_rank(self):
        ranks = [severity_rank(s) for s in Severity]
        assert len(set(ranks)) == len(ranks)


class TestMaxSeverity:
    def test_empty_iterable_returns_none(self):
        assert max_severity([]) is Severity.NONE

    def test_single_value_returns_itself(self):
        assert max_severity([Severity.SAFE]) is Severity.SAFE

    def test_returns_the_highest_of_several(self):
        assert (
            max_severity([Severity.SAFE, Severity.BREAKING, Severity.NONE])
            is Severity.BREAKING
        )

    def test_unknown_wins_over_controlled(self):
        assert (
            max_severity([Severity.CONTROLLED, Severity.UNKNOWN]) is Severity.UNKNOWN
        )

    def test_breaking_is_never_masked_by_unknown(self):
        assert (
            max_severity([Severity.UNKNOWN, Severity.BREAKING]) is Severity.BREAKING
        )

    def test_accepts_a_generator(self):
        assert (
            max_severity(s for s in (Severity.NONE, Severity.CONTROLLED))
            is Severity.CONTROLLED
        )

    def test_accepts_plain_strings(self):
        assert max_severity(["safe", "breaking"]) is Severity.BREAKING


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class TestFinding:
    def test_carries_severity_operation_and_reason(self):
        finding = Finding(
            severity=Severity.BREAKING,
            operation="DROP COLUMN",
            reason="Coluna removida.",
        )
        assert finding.severity is Severity.BREAKING
        assert finding.operation == "DROP COLUMN"
        assert finding.reason == "Coluna removida."

    def test_is_frozen(self):
        finding = Finding(Severity.SAFE, "CREATE TABLE", "Tabela criada.")
        with pytest.raises(dataclasses.FrozenInstanceError):
            finding.severity = Severity.BREAKING

    def test_compares_by_value(self):
        a = Finding(Severity.SAFE, "CREATE TABLE", "Tabela criada.")
        b = Finding(Severity.SAFE, "CREATE TABLE", "Tabela criada.")
        assert a == b


class TestWorst:
    def test_empty_returns_none(self):
        assert worst([]) is None

    def test_returns_the_finding_not_just_the_severity(self):
        findings = [
            Finding(Severity.SAFE, "CREATE TABLE", "Tabela criada."),
            Finding(Severity.BREAKING, "DROP COLUMN", "Coluna removida."),
        ]
        assert worst(findings) is findings[1]

    def test_unknown_wins_over_controlled(self):
        findings = [
            Finding(Severity.CONTROLLED, "ADD CONSTRAINT", "Constraint adicionada."),
            Finding(Severity.UNKNOWN, "ALTER TYPE", "Não sei dizer."),
        ]
        assert worst(findings) is findings[1]

    def test_breaking_is_never_masked_by_unknown(self):
        findings = [
            Finding(Severity.UNKNOWN, "ALTER TYPE", "Não sei dizer."),
            Finding(Severity.BREAKING, "DROP TABLE", "Tabela removida."),
        ]
        assert worst(findings) is findings[1]

    def test_tie_keeps_the_first_which_is_the_one_earlier_in_the_statement(self):
        findings = [
            Finding(Severity.BREAKING, "DROP COLUMN", "Primeira."),
            Finding(Severity.BREAKING, "RENAME", "Segunda."),
        ]
        assert worst(findings) is findings[0]

    def test_accepts_a_generator(self):
        findings = (
            Finding(s, "op", "razão") for s in (Severity.NONE, Severity.CONTROLLED)
        )
        assert worst(findings).severity is Severity.CONTROLLED


# ---------------------------------------------------------------------------
# SEVERITY_META — invariantes de tabela
# ---------------------------------------------------------------------------

FIELDS = tuple(field.name for field in dataclasses.fields(Presentation))


class TestSeverityMetaTable:
    def test_every_severity_has_a_row(self):
        # Uma severidade sem linha é o bug desta entrega: `unknown` não existia
        # em nenhuma das duas tabelas e caía no default cinza dos dois callers.
        assert set(SEVERITY_META) == set(Severity)

    def test_rows_are_in_severity_order(self):
        # A tabela é o que o time de dados vai ler em QQ-2162. Fora de ordem,
        # ela deixa de responder "o que é pior que o quê" de bater o olho.
        assert list(SEVERITY_META) == sorted(SEVERITY_META, key=severity_rank)

    @pytest.mark.parametrize("severity", list(Severity))
    def test_every_row_is_reachable_through_the_accessor(self, severity):
        assert presentation(severity) is SEVERITY_META[severity]

    @pytest.mark.parametrize("severity", list(Severity))
    def test_accessor_accepts_the_plain_string_too(self, severity):
        assert presentation(severity.value) is SEVERITY_META[severity]

    @pytest.mark.parametrize("severity", list(Severity))
    @pytest.mark.parametrize("field", FIELDS)
    def test_no_field_is_empty(self, severity, field):
        assert getattr(SEVERITY_META[severity], field).strip()

    @pytest.mark.parametrize("field", ("emoji", "label", "color"))
    def test_no_two_severities_share_an_identifying_field(self, field):
        # `headline` fica de fora de propósito: é a mesma frase para as três
        # severidades que descrevem uma classificação de verdade. Emoji, rótulo
        # e cor são o que distingue as linhas para quem lê o Slack.
        values = [getattr(meta, field) for meta in SEVERITY_META.values()]
        assert len(set(values)) == len(values)

    def test_the_three_classified_severities_share_one_headline(self):
        classified = (Severity.SAFE, Severity.CONTROLLED, Severity.BREAKING)
        assert len({SEVERITY_META[s].headline for s in classified}) == 1

    def test_none_and_unknown_do_not_borrow_that_headline(self):
        shared = SEVERITY_META[Severity.SAFE].headline
        assert SEVERITY_META[Severity.NONE].headline != shared
        assert SEVERITY_META[Severity.UNKNOWN].headline != shared

    def test_severity_outside_the_table_raises_instead_of_defaulting(self):
        with pytest.raises(UnknownSeverity):
            presentation("catastrophic")

    def test_missing_severity_raises_instead_of_defaulting(self):
        with pytest.raises(UnknownSeverity):
            presentation(None)

    def test_unknown_severity_is_still_a_value_error(self):
        # Herda de ValueError para não quebrar quem já capturava o que
        # `Severity(...)` levantava.
        assert issubclass(UnknownSeverity, ValueError)

    def test_missing_row_raises_instead_of_borrowing_another_row(self, monkeypatch):
        # Pareado com `test_every_severity_has_a_row`: aquele garante que a
        # tabela está completa, este garante que o acessor não disfarça uma
        # linha faltando devolvendo a de outra severidade — que é literalmente
        # o bug que `unknown` tinha nas duas tabelas antigas.
        monkeypatch.delitem(SEVERITY_META, Severity.UNKNOWN)
        with pytest.raises(UnknownSeverity):
            presentation(Severity.UNKNOWN)


class TestUnknownIsAFirstClassRow:
    def test_has_its_own_emoji(self):
        others = {SEVERITY_META[s].emoji for s in Severity if s is not Severity.UNKNOWN}
        assert SEVERITY_META[Severity.UNKNOWN].emoji not in others

    def test_has_its_own_label(self):
        others = {SEVERITY_META[s].label for s in Severity if s is not Severity.UNKNOWN}
        assert SEVERITY_META[Severity.UNKNOWN].label not in others

    def test_has_its_own_color(self):
        others = {SEVERITY_META[s].color for s in Severity if s is not Severity.UNKNOWN}
        assert SEVERITY_META[Severity.UNKNOWN].color not in others

    def test_label_does_not_read_as_a_classification(self):
        # "Mudança Controlada" e "Safe Change" afirmam algo sobre a migração.
        # A linha de `unknown` tem que afirmar o contrário: que não se sabe.
        assert SEVERITY_META[Severity.UNKNOWN].label == "Não classificado"

    def test_headline_says_the_classifier_did_not_understand(self):
        headline = SEVERITY_META[Severity.UNKNOWN].headline
        assert "não entendeu" in headline
        assert headline != SEVERITY_META[Severity.BREAKING].headline
        assert headline != SEVERITY_META[Severity.SAFE].headline
