"""Redacción de secretos en logs.

SecretRegistry centraliza los valores sensibles del proceso (api_hash,
teléfono, strings de sesión, códigos 2FA) y RedactingFilter los sustituye
en cada registro de logging antes de llegar a consola o archivo.
"""

from __future__ import annotations

import logging

REDACTED = "***REDACTED***"
_MIN_SECRET_LEN = 4


class SecretRegistry:
    """Registro de valores sensibles con redacción por sustitución."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, value: str | int | None) -> None:
        """Registra un valor sensible; ignora None/vacíos/cortos."""
        text = str(value) if value is not None else ""
        if len(text) >= _MIN_SECRET_LEN:
            self._secrets.add(text)

    def redact(self, text: str) -> str:
        """Sustituye cualquier secreto registrado por ***REDACTED***."""
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def __len__(self) -> int:
        return len(self._secrets)


_registry = SecretRegistry()


def register_secret(value: str | int | None) -> None:
    """Registra un secreto en el registro global del proceso."""
    _registry.register(value)


def redact(text: str) -> str:
    """Redacta contra el registro global del proceso."""
    return _registry.redact(text)


class RedactingFilter(logging.Filter):
    """Filtro de logging: redacta el mensaje final y anula los args.

    Se aplica sobre el mensaje YA formateado (getMessage) para no perder
    secretos que llegan vía parámetros de interpolación; los args se anulan
    para evitar el doble formateo de cadenas con '%'.
    """

    def __init__(self, registry: SecretRegistry | None = None) -> None:
        super().__init__()
        self._registry = registry if registry is not None else _registry

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._registry.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            # el traceback se formatea DESPUÉS por el Formatter, sin pasar por
            # ningún filter: mensajes de excepción con datos de usuario
            # (str(exc)) escapaban sin redactar (2ª auditoría). Se pre-formatea,
            # redacta y fija como exc_text.
            exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._registry.redact(exc_text)
            record.exc_info = None
        return True
