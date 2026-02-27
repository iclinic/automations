"""
Logging handler que emite GitHub Actions workflow commands.

Mapeamento de níveis:
  DEBUG    → ::debug::      (visível apenas com "Enable debug logging")
  INFO     → saída limpa em stdout
  WARNING  → ::warning::    (anotação amarela na PR / resumo do workflow)
  ERROR    → ::error::      (anotação vermelha na PR / resumo do workflow)
  CRITICAL → ::error::      (idem)

Uso:
    from gha_logger import get_logger
    log = get_logger(__name__)
    log.info("arquivo lido com sucesso")
    log.warning("arquivo ignorado")
    log.error("falha na chamada à IA")
"""

import logging
import os
import sys


class _GHAHandler(logging.Handler):
    """Formata registros como workflow commands do GitHub Actions."""

    _LEVEL_PREFIX = {
        logging.DEBUG:    "::debug::",
        logging.WARNING:  "::warning::",
        logging.ERROR:    "::error::",
        logging.CRITICAL: "::error::",
    }

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        prefix = self._LEVEL_PREFIX.get(record.levelno)
        if prefix:
            print(f"{prefix}{msg}", file=sys.stderr, flush=True)
        else:
            # INFO e níveis intermediários: saída limpa no stdout
            print(msg, flush=True)


def get_logger(name: str = "migration-detector") -> logging.Logger:
    """Retorna um logger configurado para GitHub Actions.

    Em ambientes fora do GHA (ex.: testes locais) o comportamento é idêntico,
    apenas sem os prefixos de workflow command — o que facilita os testes.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # evita registrar o handler mais de uma vez

    logger.setLevel(logging.DEBUG)
    handler = _GHAHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


class group:
    """Context manager que envolve um bloco de log com ::group:: / ::endgroup::.

    Exemplo:
        with group("Lendo arquivos de migração"):
            log.info("arquivo.sql lido")
    """

    def __init__(self, title: str) -> None:
        self.title = title

    def __enter__(self) -> "group":
        print(f"::group::{self.title}", flush=True)
        return self

    def __exit__(self, *_) -> None:
        print("::endgroup::", flush=True)
