"""Tests de logging con redacción y rotación. Sin red, sin Telegram."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

from tg_sentry import redact
from tg_sentry.logging_setup import setup_logging


def test_setup_logging_crea_dir_0700_y_log_0600(tmp_path: Path) -> None:
    log_file = setup_logging(logging.INFO, log_dir=tmp_path)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert log_file == tmp_path / "tg-sentry.log"
    logging.getLogger("test").info("hola")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hola" in log_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_log_redacta_secretos_registrados(tmp_path: Path) -> None:
    redact.register_secret("SECRETO-SUPER-LARGO")
    log_file = setup_logging(logging.INFO, log_dir=tmp_path)
    logging.getLogger("test").info("valor=SECRETO-SUPER-LARGO en operación")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "SECRETO-SUPER-LARGO" not in content
    assert redact.REDACTED in content


def test_setup_logging_silencia_telethon_a_warning(tmp_path: Path) -> None:
    import logging as _logging

    setup_logging(_logging.INFO, log_dir=tmp_path)
    assert _logging.getLogger("telethon").getEffectiveLevel() == _logging.WARNING


def test_setup_logging_reconfigurable_sin_duplicar_handlers(tmp_path: Path) -> None:
    setup_logging(logging.INFO, log_dir=tmp_path / "uno")
    n_handlers = len(logging.getLogger().handlers)
    setup_logging(logging.INFO, log_dir=tmp_path / "dos")
    assert len(logging.getLogger().handlers) == n_handlers
    assert (tmp_path / "dos" / "tg-sentry.log").exists()
