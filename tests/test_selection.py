"""Tests del modelo de selección de la TUI (puro, sin Textual). Sin red."""

from __future__ import annotations

from tests.test_inventory import _to_infos
from tg_sentry.models import DialogInfo, PeerKind, PeerRole
from tg_sentry.tui.selection import SelectionModel


def test_blindados_no_se_pueden_seleccionar() -> None:
    modelo = SelectionModel(me_id=1, whitelist=["999"])
    dialogs = _to_infos()
    por_id = {d.id: d for d in dialogs}
    # 555 es unknown → blindado
    assert modelo.toggle(por_id[555]) is False
    assert len(modelo) == 0
    assert "desconocido" in (modelo.lock_reason(por_id[555]) or "")


def test_toggle_y_deseleccion() -> None:
    modelo = SelectionModel(me_id=1)
    dialogs = _to_infos()
    bot = dialogs[0]
    assert modelo.toggle(bot) is True
    assert len(modelo) == 1
    assert modelo.toggle(bot) is True
    assert len(modelo) == 0


def test_select_all_ignora_blindados() -> None:
    modelo = SelectionModel(me_id=1)
    dialogs = _to_infos()
    total = modelo.select_all(dialogs)
    assert total == 5  # 6 fakes menos el unknown blindado
    assert 555 not in modelo.selected


def test_invertir_y_limpiar() -> None:
    modelo = SelectionModel(me_id=1)
    dialogs = _to_infos()
    modelo.select_all(dialogs)
    modelo.invert(dialogs)
    assert len(modelo) == 0
    modelo.select_all(dialogs)
    modelo.clear()
    assert len(modelo) == 0


def test_selected_dialogs_conserva_orden_de_lista() -> None:
    modelo = SelectionModel(me_id=1)
    dialogs = _to_infos()
    modelo.select_all(dialogs)
    elegidos = modelo.selected_dialogs(dialogs)
    assert [d.id for d in elegidos] == [d.id for d in dialogs if d.id != 555]


def test_whitelist_por_username_bloquea() -> None:
    modelo = SelectionModel(me_id=1, whitelist=["ejemplobot"])
    dialogs = _to_infos()
    bot = dialogs[0]  # username ejemplobot
    assert modelo.toggle(bot) is False
    assert "whitelist" in (modelo.lock_reason(bot) or "")


def test_servicio_y_propia_cuenta_blindados() -> None:
    propio = DialogInfo(id=1, kind=PeerKind.USER)
    servicio = DialogInfo(id=777000, kind=PeerKind.USER, name="Telegram")
    creador = DialogInfo(id=5, kind=PeerKind.GROUP, role=PeerRole.CREATOR)
    secreto = DialogInfo(id=6, kind=PeerKind.SECRET)
    modelo = SelectionModel(me_id=1)
    for dialog in (propio, servicio, creador, secreto):
        assert modelo.toggle(dialog) is False, dialog.id


# --- Regresiones de la auditoría 2026-09 -----------------------------------


def test_sin_me_id_no_hay_nada_seleccionable() -> None:
    """Fail-closed: sin identidad propia, nada se puede marcar (P1)."""
    modelo = SelectionModel(me_id=None)
    dialogs = _to_infos()
    for dialog in dialogs:
        assert modelo.toggle(dialog) is False, dialog.id
    assert modelo.select_all(dialogs) == 0
    assert len(modelo) == 0


def test_select_all_devuelve_delta_de_la_vista() -> None:
    """El contador de la UI debe cuadrar: delta de la vista, no total global."""
    modelo = SelectionModel(me_id=1)
    dialogs = _to_infos()
    assert modelo.select_all(dialogs) == 5
    # segunda pasada sobre la MISMA vista: 0 nuevos
    assert modelo.select_all(dialogs) == 0
    assert len(modelo) == 5


def test_contactos_blindados_en_seleccion() -> None:
    modelo = SelectionModel(me_id=1, contact_ids={222}, protect_contacts=True)
    dialogs = _to_infos()
    contacto = next(d for d in dialogs if d.id == 222)
    assert modelo.toggle(contacto) is False
    assert "contacto" in (modelo.lock_reason(contacto) or "")
    # con la protección desactivada, se puede seleccionar
    modelo2 = SelectionModel(me_id=1, contact_ids={222}, protect_contacts=False)
    assert modelo2.toggle(contacto) is True
