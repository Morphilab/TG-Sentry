"""Tests de filtros combinables y regex seguro. Sin red."""

from __future__ import annotations

import pytest

from tests.test_inventory import _to_infos
from tg_sentry.filters import FilterError, Filters, apply_filters, safe_compile
from tg_sentry.models import PeerKind


def test_filtro_por_tipos() -> None:
    dialogs = _to_infos()
    resultado = apply_filters(dialogs, Filters(kinds=[PeerKind.BOT]))
    assert [d.id for d in resultado] == [111]
    resultado = apply_filters(dialogs, Filters(kinds=[PeerKind.CHANNEL, PeerKind.GROUP]))
    assert {d.kind for d in resultado} == {PeerKind.CHANNEL, PeerKind.GROUP}


def test_filtro_name_contains_case_insensitive() -> None:
    resultado = apply_filters(_to_infos(), Filters(name_contains="grupo"))
    assert [d.id for d in resultado] == [-100333]


def test_filtro_name_regex() -> None:
    resultado = apply_filters(_to_infos(), Filters(name_regex=r"^Canal\b"))
    assert [d.id for d in resultado] == [-100444, -100666]


def test_filtro_regex_invalido_lanza_filter_error() -> None:
    with pytest.raises(FilterError, match="regex inválido"):
        apply_filters(_to_infos(), Filters(name_regex="(["))


def test_safe_compile_rechaza_patron_demasiado_largo() -> None:
    with pytest.raises(FilterError, match="demasiado largo"):
        safe_compile("a" * 201)


def test_filtro_inactivos_sin_mensajes_cuenta_como_inactivo() -> None:
    # 111 tiene mensaje de 2020; 555 no tiene fecha → ambos inactivos a 90d
    resultado = apply_filters(_to_infos(), Filters(inactive_days=90))
    ids = [d.id for d in resultado]
    assert 111 in ids
    assert 555 in ids
    assert -100333 not in ids  # reciente


def test_filtro_recientes_por_descarte() -> None:
    todos = _to_infos()
    inactivos = apply_filters(todos, Filters(inactive_days=90))
    activos = [d for d in todos if d.id not in {i.id for i in inactivos}]
    assert {d.id for d in activos} == {222, -100333, -100666}


def test_filtro_archivados_y_unread_min() -> None:
    dialogs = _to_infos()
    assert [d.id for d in apply_filters(dialogs, Filters(archived=True))] == [-100333]
    # ≥1000 no leídos: grupo Ejemplo (148675) y canal Ejemplo (51532); Alertas (25) no entra
    assert {d.id for d in apply_filters(dialogs, Filters(unread_min=1000))} == {
        -100333, -100666,
    }


def test_filtros_combinados_and() -> None:
    resultado = apply_filters(
        _to_infos(),
        Filters(kinds=[PeerKind.CHANNEL], archived=False, unread_min=1000),
    )
    assert {d.id for d in resultado} == {-100666}


def test_filtro_sin_filtros_devuelve_todo() -> None:
    dialogs = _to_infos()
    assert apply_filters(dialogs, Filters()) == dialogs
