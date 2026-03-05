import logging
import sys

import pytest

from gha_logger import _GHAHandler, get_logger, group


# ---------------------------------------------------------------------------
# _GHAHandler — formatação de workflow commands
# ---------------------------------------------------------------------------


class TestGHAHandler:
    def _emit(self, level: int, message: str, capsys) -> tuple[str, str]:
        handler = _GHAHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="test", level=level, pathname="", lineno=0,
            msg=message, args=(), exc_info=None,
        )
        handler.emit(record)
        captured = capsys.readouterr()
        return captured.out, captured.err

    def test_debug_uses_workflow_command(self, capsys):
        _, err = self._emit(logging.DEBUG, "debug msg", capsys)
        assert err.strip() == "::debug::debug msg"

    def test_warning_uses_workflow_command(self, capsys):
        _, err = self._emit(logging.WARNING, "warn msg", capsys)
        assert err.strip() == "::warning::warn msg"

    def test_error_uses_workflow_command(self, capsys):
        _, err = self._emit(logging.ERROR, "error msg", capsys)
        assert err.strip() == "::error::error msg"

    def test_critical_uses_error_workflow_command(self, capsys):
        _, err = self._emit(logging.CRITICAL, "crit msg", capsys)
        assert err.strip() == "::error::crit msg"

    def test_info_goes_to_stdout_without_prefix(self, capsys):
        out, err = self._emit(logging.INFO, "info msg", capsys)
        assert out.strip() == "info msg"
        assert err == ""

    def test_info_has_no_workflow_command_prefix(self, capsys):
        out, _ = self._emit(logging.INFO, "plain", capsys)
        assert not out.startswith("::")


# ---------------------------------------------------------------------------
# get_logger — configuração
# ---------------------------------------------------------------------------


class TestGetLogger:
    def test_returns_logger_instance(self):
        logger = get_logger("test-logger-1")
        assert isinstance(logger, logging.Logger)

    def test_has_gha_handler_attached(self):
        logger = get_logger("test-logger-unique-handler")
        assert any(isinstance(h, _GHAHandler) for h in logger.handlers)

    def test_calling_twice_does_not_duplicate_handlers(self):
        name = "test-logger-dedup"
        l1 = get_logger(name)
        l2 = get_logger(name)
        gha_handlers = [h for h in l2.handlers if isinstance(h, _GHAHandler)]
        assert len(gha_handlers) == 1

    def test_propagate_is_false(self):
        logger = get_logger("test-no-propagate")
        assert logger.propagate is False

    def test_level_is_debug(self):
        logger = get_logger("test-level")
        assert logger.level == logging.DEBUG

    def test_warning_emits_to_stderr(self, capsys):
        logger = get_logger("test-warn-stderr")
        logger.warning("something wrong")
        err = capsys.readouterr().err
        assert "::warning::something wrong" in err

    def test_error_emits_to_stderr(self, capsys):
        logger = get_logger("test-err-stderr")
        logger.error("boom")
        err = capsys.readouterr().err
        assert "::error::boom" in err

    def test_info_emits_to_stdout(self, capsys):
        logger = get_logger("test-info-stdout")
        logger.info("all good")
        out = capsys.readouterr().out
        assert "all good" in out


# ---------------------------------------------------------------------------
# group — context manager
# ---------------------------------------------------------------------------


class TestGroup:
    def test_prints_group_open_and_close(self, capsys):
        with group("My Section"):
            pass
        out = capsys.readouterr().out
        assert "::group::My Section" in out
        assert "::endgroup::" in out

    def test_group_open_before_close(self, capsys):
        with group("Order Check"):
            pass
        out = capsys.readouterr().out
        open_pos  = out.index("::group::Order Check")
        close_pos = out.index("::endgroup::")
        assert open_pos < close_pos

    def test_body_executes_inside_group(self, capsys):
        executed = []
        with group("exec"):
            executed.append(True)
        assert executed == [True]

    def test_endgroup_called_even_on_exception(self, capsys):
        with pytest.raises(ValueError):
            with group("err section"):
                raise ValueError("oops")
        out = capsys.readouterr().out
        assert "::endgroup::" in out
