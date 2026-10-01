"""Tests de redacción de secretos. Sin red, sin Telegram."""

from __future__ import annotations

import logging

from tg_sentry.redact import REDACTED, RedactingFilter, SecretRegistry


def test_registry_redacta_valores_registrados() -> None:
    registry = SecretRegistry()
    registry.register("deadbeefdeadbeef")
    text = "conectando con api_hash=deadbeefdeadbeef ok"
    assert registry.redact(text) == f"conectando con api_hash={REDACTED} ok"


def test_registry_ignora_vacios_y_cortos() -> None:
    registry = SecretRegistry()
    registry.register(None)
    registry.register("")
    registry.register("abc")
    registry.register(123)
    assert len(registry) == 0


def test_registry_acepta_enteros_largos() -> None:
    registry = SecretRegistry()
    registry.register(123456789)
    assert "123456789" not in registry.redact("telefono 123456789")


def test_filter_redacta_msg_y_anula_args() -> None:
    registry = SecretRegistry()
    registry.register("+34600000000")
    filtro = RedactingFilter(registry)
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="llamando al %s", args=("+34600000000",), exc_info=None,
    )
    assert filtro.filter(record) is True
    assert record.getMessage() == f"llamando al {REDACTED}"
    assert record.args == ()


def test_traceback_tambien_se_redacta() -> None:
    """2ª auditoría: el exc_info se formatea DESPUÉS del filter — mensajes de
    excepción con secretos (str(exc)) escapaban sin redactar."""
    import io
    import logging as _logging

    registry = SecretRegistry()
    registry.register("hashsecreto123")
    stream = io.StringIO()
    handler = _logging.StreamHandler(stream)
    handler.setFormatter(_logging.Formatter("%(message)s\\n%(exc_text)s"))
    handler.addFilter(RedactingFilter(registry))
    logger = _logging.Logger("prueba-traza")
    logger.addHandler(handler)

    try:
        raise ValueError("fallo con hashsecreto123 dentro")
    except ValueError:
        logger.exception("algo reventó")

    salida = stream.getvalue()
    assert "hashsecreto123" not in salida
    assert "***REDACTED***" in salida
