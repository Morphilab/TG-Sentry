"""Tests de la TUI con el pilot de Textual (headless, sin red).

Requiere caché+config en XDG temporal y SIN sesión (LoginScreen) o CON
sesión ficticia (Dashboard). Nunca conecta: el dashboard lee solo caché.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_inventory import _to_infos
from tg_sentry import inventory
from tg_sentry.config import Config, save_config
from tg_sentry.models import PeerKind
from tg_sentry.tui.app import SentryApp


@pytest.fixture()
def entorno(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_file = tmp_path / "conf" / "tg-sentry" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    save_config(Config(), config_file)
    inventory.store(_to_infos(), inventory.db_path(), me_id=42)
    # sesión ficticia para que la app arranque en dashboard
    sesion = tmp_path / "data" / "tg-sentry" / "tg-sentry.session"
    sesion.parent.mkdir(parents=True, exist_ok=True)
    sesion.write_bytes(b"sqlite-ficticia")
    return tmp_path


async def test_dashboard_arranca_y_filtra(entorno: Path) -> None:
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from tg_sentry.tui.screens.dashboard import DashboardScreen

        assert isinstance(app.screen, DashboardScreen)
        tabla = app.screen.query_one("#tabla")
        # sin filtros de tipo activos salvo bot (default) → 1 bot visible
        assert tabla.row_count == 1
        # activar grupos → 2 filas (grupo + bot)
        app.screen.query_one("#f-group").value = True
        await pilot.pause()
        assert tabla.row_count == 2


async def test_marcar_no_mueve_el_cursor(entorno: Path) -> None:
    # regresión: reconstruir la tabla en cada marca reseteaba el cursor
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dashboard = app.screen
        dashboard.query_one("#f-group").value = True  # 2 filas
        await pilot.pause()
        tabla = dashboard.query_one("#tabla")
        tabla.move_cursor(row=1)
        await pilot.pause()
        assert tabla.cursor_row == 1
        # espacio marca/desmarca → el cursor SIGUE en la fila 1
        await pilot.press("space")
        await pilot.pause()
        assert tabla.cursor_row == 1
        await pilot.press("space")
        await pilot.pause()
        assert tabla.cursor_row == 1
        # selección masiva tampoco lo mueve
        await pilot.press("a")
        await pilot.pause()
        assert tabla.cursor_row == 1
        # y el cambio de filtros (reconstrucción) lo PRESERVA
        dashboard.query_one("#f-user").value = True
        await pilot.pause()
        assert tabla.cursor_row == 1


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)  # ver nota al pie del test
async def test_seleccion_masiva_y_bloqueo_de_blindados(entorno: Path) -> None:
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dashboard = app.screen
        dashboard.query_one("#f-group").value = True
        dashboard.query_one("#f-user").value = True
        await pilot.pause()
        # 'a' selecciona todo lo filtrado (el unknown no pasa el filtro de tipos)
        await pilot.press("a")
        assert len(dashboard.selection) >= 3
        # blindado: activar unknown no es posible por filtro; probamos lock directo
        from tg_sentry.models import DialogInfo

        unknown = DialogInfo(id=555, kind=PeerKind.UNKNOWN)
        assert dashboard.selection.toggle(unknown) is False
        await pilot.press("n")
        assert len(dashboard.selection) == 0


async def test_flujo_hasta_confirmacion(entorno: Path) -> None:
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dashboard = app.screen
        await pilot.press("a")
        assert len(dashboard.selection) >= 1
        await pilot.press("p")
        await pilot.pause()
        from tg_sentry.tui.screens.plan import PlanScreen

        assert isinstance(app.screen, PlanScreen)
        await pilot.press("c")
        await pilot.pause()
        from tg_sentry.tui.screens.confirm import ConfirmScreen

        assert isinstance(app.screen, ConfirmScreen)
        # el botón ejecutar nace deshabilitado y solo CONFIRMAR lo habilita
        boton = app.screen.query_one("#btn-ejecutar")
        assert boton.disabled is True
        app.screen.query_one("#palabra").value = "CONFIRM"
        await pilot.pause()
        assert boton.disabled is True
        app.screen.query_one("#palabra").value = "CONFIRMAR"
        await pilot.pause()
        assert boton.disabled is False
        # cancelar vuelve al dashboard sin ejecutar nada
        app.screen.query_one("#btn-cancelar").press()
        await pilot.pause()
        from tg_sentry.tui.screens.dashboard import DashboardScreen

        assert isinstance(app.screen, DashboardScreen)


async def test_enter_con_confirmar_lanza_ejecucion(entorno: Path, monkeypatch) -> None:
    # regresión: teclear CONFIRMAR y pulsar Enter debía ejecutar (Input.Submitted)
    app = SentryApp()

    async def _sin_cliente(_self):
        raise RuntimeError("sin conexión en test")

    monkeypatch.setattr(SentryApp, "get_client", _sin_cliente)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        from tg_sentry.tui.screens.confirm import ConfirmScreen

        assert isinstance(app.screen, ConfirmScreen)
        campo = app.screen.query_one("#palabra")
        assert campo.has_focus  # AUTO_FOCUS: se puede teclear directamente
        campo.value = "CONFIRMAR"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        from tg_sentry.tui.screens.execute import ExecuteScreen

        assert isinstance(app.screen, ExecuteScreen)
        # sin conexión (falseada): error visible en el estado app-level
        await pilot.pause(1.0)  # el interval de la vista pinta el estado
        assert app.ejecucion.estado == "error"
        assert any("sin conexión" in linea for linea in app.ejecucion.log)


async def test_execute_plan_expirado_mensaje_claro(entorno: Path, monkeypatch) -> None:
    # regresión: un plan vencido mataba el worker en silencio (pantalla vacía)
    from datetime import UTC, datetime, timedelta

    from tg_sentry.models import DialogInfo, PeerKind
    from tg_sentry.planner import PlannerOptions, build_plan
    from tg_sentry.tui.screens.execute import ExecuteScreen

    app = SentryApp()

    async def _cliente_falso(_self):
        return object()

    monkeypatch.setattr(SentryApp, "get_client", _cliente_falso)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=99, kind=PeerKind.BOT, name="b")], PlannerOptions(ttl_min=1), me_id=42
        )
        expirado = plan.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(minutes=2)}
        )
        app.push_screen(ExecuteScreen(expirado))
        await pilot.pause(1.0)
        assert app.ejecucion.estado == "error"
        assert "EXPIRÓ" in app.ejecucion.mensaje


# NOTA (3ª auditoría): este test dispara, en algunas corridas, un
# PytestUnraisableExceptionWarning con la coroutine _aplicar_worker: artefacto
# de timing de los workers `exclusive` de Textual en run_test (una coroutine
# reemplazada antes de arrancar se cierra sin await). Inocuo y solo de test;
# en producción el guard is_mounted cubre el desmontaje real.
