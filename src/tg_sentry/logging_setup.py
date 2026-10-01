"""Logging con rotación y redacción.

Consola + RotatingFileHandler (10 MB x 5 backups) bajo el directorio XDG de
estado (0700). Ambos handlers llevan RedactingFilter: ningún secreto
registrado llega al log. La configuración es re-entrant: llamarla de nuevo
sustituye los handlers propios (útil en tests) sin tocar handlers ajenos.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

import tg_sentry.paths as paths
from tg_sentry.redact import RedactingFilter

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

_owned_handlers: list[logging.Handler] = []


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler que fuerza 0600 en cada archivo que abre.

    La rotación crea archivos nuevos con permisos de umask (0644 típico);
    aunque el directorio ya es 0700, se fuerza por defensa en profundidad.
    """

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def setup_logging(level: int = logging.INFO, log_dir: Path | None = None) -> Path:
    """Configura el root logger con consola + archivo rotado redactado.

    Devuelve la ruta del archivo de log activo.
    """
    directory = log_dir if log_dir is not None else paths.logs_dir()
    paths.ensure_private_dir(directory)
    file_path = directory / "tg-sentry.log"

    root = logging.getLogger()
    for handler in _owned_handlers:
        root.removeHandler(handler)
        handler.close()
    _owned_handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(RedactingFilter())

    file_handler = _PrivateRotatingFileHandler(
        file_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactingFilter())

    root.addHandler(console)
    root.addHandler(file_handler)
    _owned_handlers.extend([console, file_handler])
    root.setLevel(level)
    # Telethon loguea conexión/paquetes a INFO — ruido total en consola;
    # sus eventos relevantes para nosotros llegan como WARNING+.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    return file_path


def owned_handlers() -> Iterable[logging.Handler]:
    """Handlers gestionados por este módulo (para inspección en tests)."""
    return list(_owned_handlers)
