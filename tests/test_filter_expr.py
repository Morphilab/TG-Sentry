"""Tests del parser de expresiones de filtro del CLI. Sin red."""

from __future__ import annotations

import pytest

from tg_sentry.filters import FilterError, Filters, PeerKind, parse_filter_expr


def test_expresion_compleja() -> None:
    filtros = parse_filter_expr(
        "kinds=bot+channel,regex=^RT,inactive=90,archived=any,unread_min=100"
    )
    assert filtros.kinds == [PeerKind.BOT, PeerKind.CHANNEL]
    assert filtros.name_regex == "^RT"
    assert filtros.inactive_days == 90
    assert filtros.archived is None
    assert filtros.unread_min == 100


def test_expresion_vacia_devuelve_filtros_neutros() -> None:
    assert parse_filter_expr("") == Filters()


def test_archived_true_false() -> None:
    assert parse_filter_expr("archived=true").archived is True
    assert parse_filter_expr("archived=false").archived is False


def test_clave_desconocida() -> None:
    with pytest.raises(FilterError, match="desconocida"):
        parse_filter_expr("tipo=bot")


def test_tipo_invalido_lista_validos() -> None:
    with pytest.raises(FilterError, match="tipo inválido"):
        parse_filter_expr("kinds=robot")


def test_inactive_no_entero() -> None:
    with pytest.raises(FilterError, match="entero"):
        parse_filter_expr("inactive=noventa")


def test_malformado_sin_igual() -> None:
    with pytest.raises(FilterError, match="clave=valor"):
        parse_filter_expr("bots")


def test_role_valido_e_invalido() -> None:
    from tg_sentry.models import PeerRole

    assert parse_filter_expr("role=admin").role is PeerRole.ADMIN
    with pytest.raises(FilterError, match="rol inválido"):
        parse_filter_expr("role=jefe")


def test_clave_duplicada_rechazada() -> None:
    """2ª auditoría: inactive=5,inactive=90 ganaba la última en silencio."""
    with pytest.raises(FilterError, match="duplicada"):
        parse_filter_expr("inactive=5,inactive=90")


def test_valor_vacio_rechazado() -> None:
    with pytest.raises(FilterError, match="valor vacío"):
        parse_filter_expr("name=")


def test_inactive_fuera_de_rango_es_filtererror_no_validationerror() -> None:
    """2ª auditoría: inactive=-5 pasaba el parseo y reventaba con
    ValidationError de pydantic (traceback crudo en el CLI)."""
    with pytest.raises(FilterError):
        parse_filter_expr("inactive=-5")
