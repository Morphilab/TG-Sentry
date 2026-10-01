"""Tests M6.1: la ejecución vive en la app y sobrevive al cambio de pantalla.

Regresión del hallazgo: los workers de una Screen de Textual son cancelados
al desmontarla — "Volver al dashboard" mataba la cola aunque dijera "no
aborta". Ahora la tarea vive en SentryApp y la pantalla es una vista.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_inventory import _to_infos
from tg_sentry import inventory
from tg_sentry.audit import AuditRecord, StepOutcome
from tg_sentry.config import Config, save_config
from tg_sentry.executor import RunResult
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import PlannerOptions, build_plan
from tg_sentry.tui.app import SentryApp
from tg_sentry.tui.ejecucion import Ejecucion
from tg_sentry.tui.screens.dashboard import DashboardScreen
from tg_sentry.tui.screens.execute import ExecuteScreen


@pytest.fixture()
def entorno(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_file = tmp_path / "conf" / "tg-sentry" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    save_config(Config(), config_file)
    inventory.store(_to_infos(), inventory.db_path(), me_id=42)
    sesion = tmp_path / "data" / "tg-sentry" / "tg-sentry.session"
    sesion.parent.mkdir(parents=True, exist_ok=True)
    sesion.write_bytes(b"sqlite-ficticia")
    return tmp_path


class _FakeEngineLento:
    """Motor falso con trabajo real: ejecuta en varios ticks del loop."""

    paso_s = 0.3  # cada test puede volverlo más lento (clase compartida)

    def __init__(self, gateway, limits, audit, *, sleep=None, sleep_budget=None, clock=None):
        self.audit = audit

    async def run(self, plan, *, completed=None, after_step=None, max_peers=None):
        for peer in plan.peers:
            for step in peer.steps:
                await asyncio.sleep(self.paso_s)  # trabajo simulado
                self.audit.record(
                    AuditRecord(
                        ts=datetime.now(UTC),
                        plan_id=plan.plan_id,
                        peer_id=peer.dialog.id,
                        step=step.kind.value,
                        outcome=StepOutcome.DONE,
                    )
                )
                if after_step is not None:
                    after_step(peer.dialog.id, step.kind, StepOutcome.DONE)
        return RunResult(done=len(plan.peers) * 2)


@pytest.fixture(autouse=True)
def _velocidad_motor_reset():
    """Los tests ajustan paso_s de la clase compartida; se resetea entre tests."""
    yield
    _FakeEngineLento.paso_s = 0.3


class _FakeGateway:
    def __init__(self, runtime, client) -> None:
        pass

    async def run_step(self, step, dialog) -> None:  # type: ignore[no-untyped-def]
        return None


class _FakeRuntime:
    async def call(self, coro):  # type: ignore[no-untyped-def]
        return await coro


class _FakeApp(SentryApp):
    async def get_client(self):  # type: ignore[no-untyped-def]
        return object()

    @property
    def runtime(self):  # type: ignore[no-untyped-def]
        return _FakeRuntime()


@pytest.fixture()
def app_con_motor_falso(monkeypatch):
    monkeypatch.setattr("tg_sentry.tui.app.ExecutionEngine", _FakeEngineLento)
    return _FakeApp


async def test_ejecucion_sobrevive_al_cambio_de_pantalla(
    entorno: Path, app_con_motor_falso
) -> None:
    # EL fix: cambiar de pantalla a mitad de la ejecución NO la mata
    app = app_con_motor_falso()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=500 + i, kind=PeerKind.BOT, name=f"b{i}") for i in range(3)],
            PlannerOptions(),
            me_id=42,
        )
        # arranca la ejecución (6 pasos x 0.3s con el fake)
        app.push_screen(ExecuteScreen(plan))
        await pilot.pause(0.2)

        # vuelve al dashboard A MITAD de la ejecución (antes: mataba la cola)
        app.switch_screen(DashboardScreen())
        await pilot.pause(0.05)
        assert app.ejecucion is not None
        assert app.ejecucion.estado == "corriendo"

        # la ejecución SIGUE avanzando sin pantalla delante
        await pilot.pause(2.5)
        assert app.ejecucion.estado == "fin"
        assert app.ejecucion.resultado is not None
        assert app.ejecucion.resultado.done == 6  # 3 bots x (delete+block)

        # y todo quedó auditado
        registros = app.ejecucion  # el log buffer tiene los pasos
        assert len([linea for linea in registros.log if "done" in linea]) >= 6


async def test_pantalla_se_readjunta_a_ejecucion_activa(entorno: Path, app_con_motor_falso) -> None:
    _FakeEngineLento.paso_s = 0.6  # aún activa cuando re-adjunta
    app = app_con_motor_falso()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=500 + i, kind=PeerKind.BOT, name=f"b{i}") for i in range(2)],
            PlannerOptions(),
            me_id=42,
        )
        app.push_screen(ExecuteScreen(plan))
        await pilot.pause(0.2)
        app.switch_screen(DashboardScreen())  # sales a mitad
        await pilot.pause(0.05)

        # 'e' en el dashboard re-abre la vista y se readjunta (no reinicia)
        await pilot.press("e")
        await pilot.pause(0.05)
        assert isinstance(app.screen, ExecuteScreen)
        estado = str(app.screen.query_one("#estado").render())
        assert "corriendo" in estado or "fin" in estado
        await pilot.pause(3.0)
        assert app.ejecucion.estado == "fin"
        # una sola ejecución: la vista re-adjunta NO relanzó nada
        assert app.ejecucion.resultado.done == 4


async def test_no_permite_dos_ejecuciones_simultaneas(entorno: Path, app_con_motor_falso) -> None:
    app = app_con_motor_falso()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=500 + i, kind=PeerKind.BOT, name=f"b{i}") for i in range(3)],
            PlannerOptions(),
            me_id=42,
        )
        primera = app.iniciar_ejecucion(plan)
        segunda = app.iniciar_ejecucion(plan)
        assert primera is segunda  # la activa se devuelve, no se duplica
        await asyncio.sleep(0)
        await pilot.pause(2.5)
        assert primera.estado == "fin"


async def test_dashboard_tecla_e_sin_ejecucion_avisa(entorno: Path) -> None:
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.ejecucion is None
        await pilot.press("e")
        await pilot.pause()
        # sigue en dashboard y la app no reventó
        assert isinstance(app.screen, DashboardScreen)


async def test_eta_incluye_cooldowns_estimados(entorno: Path, monkeypatch) -> None:
    # regresión: el ETA callaba los cooldowns (97 min "eran" 97 + horas de cooldown)
    from tg_sentry.tui.screens.execute import ExecuteScreen

    class _GatewaySinTrabajo:
        def __init__(self, runtime, client) -> None:
            pass

        async def run_step(self, step, dialog) -> int | None:
            return None  # sin conteo: cooldown conservador

    class _Rt:
        async def call(self, coro):  # type: ignore[no-untyped-def]
            return await coro

    class _App(SentryApp):
        async def get_client(self):  # type: ignore[no-untyped-def]
            return object()

        @property
        def runtime(self):  # type: ignore[no-untyped-def]
            return _Rt()

    monkeypatch.setattr("tg_sentry.tui.app.BridgedGateway", _GatewaySinTrabajo)
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [
                DialogInfo(id=500, kind=PeerKind.BOT, name="b"),
                DialogInfo(id=600, kind=PeerKind.GROUP, name="g1"),
                DialogInfo(id=601, kind=PeerKind.GROUP, name="g2"),
            ],
            PlannerOptions(delete_own="me", leave=False),
            me_id=42,
        )
        # 1 bot (1 paso: delete_own, leave=False) + 2 grupos (1 c/u): tras el
        # 1º de borrado hay cooldown (quedan 2); tras el último no (M6.3)
        app.push_screen(ExecuteScreen(plan))
        await pilot.pause(0.7)  # el paso delete_own ya corrió: cooldown EN CURSO
        assert app.ejecucion.cooldown_hasta is not None
        eta = str(app.screen.query_one("#eta").render())
        assert "cooldowns" in eta
        # gateway sin conteo → techo conservador: 1200 en curso + 2 restantes
        # x [60s piso, 1200 techo] -> 22-60 min (el bot ahora SÍ lleva delete_own)
        assert "22-60" in eta
        # el grupo está en "esperando 1200s" (conservador, sin conteo)
        assert any("1200s" in linea for linea in app.ejecucion.log)
        # cancelo la tarea (equivale a cerrar tg-sentry): estado abortada
        tarea = app.ejecucion.tarea
        assert tarea is not None
        tarea.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await tarea
        assert app.ejecucion.estado == "abortada"


# --- Regresiones de la auditoría 2026-09 (rama TUI) ------------------------


async def test_tecla_e_sobre_ejecucion_terminada_no_relanza(
    entorno: Path, app_con_motor_falso
) -> None:
    """P1: 'e' es una vista. Sobre un plan fin/abortado ADJUNTA (mismo objeto,
    resultado preservado), jamás crea una ejecución nueva sin CONFIRMAR."""
    app = app_con_motor_falso()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=700, kind=PeerKind.BOT, name="b")],
            PlannerOptions(),
            me_id=42,
        )
        primera = app.iniciar_ejecucion(plan)
        await pilot.pause(1.5)
        assert primera.estado == "fin"
        resultado_original = primera.resultado
        # 'e' desde el dashboard: re-abre la vista de una ejecución TERMINADA
        await pilot.press("e")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ExecuteScreen)
        assert app.ejecucion is primera  # misma ejecución, no una nueva
        assert app.ejecucion.resultado is resultado_original
        await pilot.pause(1.0)
        assert app.ejecucion is primera  # sigue sin relanzar


async def test_iniciar_con_limites_subidos_se_niega(entorno: Path, monkeypatch) -> None:
    """P2: la TUI aplica validate_policy (mismo gate que el CLI)."""
    from tg_sentry.config import load_config

    cfg = load_config()
    cfg.limits.writes_per_min = 12  # subido a mano: exige --ack-over-limit
    app = SentryApp(config=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=800, kind=PeerKind.BOT, name="b")],
            PlannerOptions(),
            me_id=42,
        )
        e = app.iniciar_ejecucion(plan)
        await pilot.pause(0.2)
        assert e.estado == "error"
        assert "límites" in e.mensaje.lower() or "TOML" in e.mensaje


async def test_iniciar_sin_me_id_no_arranca(entorno: Path, monkeypatch) -> None:
    """P1: sin identidad en la caché, la ejecución se niega (fail-closed)."""
    import sqlite3

    from tg_sentry import paths as app_paths

    db = app_paths.data_dir() / "inventory.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("DELETE FROM meta WHERE key = 'me_id'")
    conn.close()

    app = _FakeApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=810, kind=PeerKind.BOT, name="b")],
            PlannerOptions(),
            me_id=42,
        )
        e = app.iniciar_ejecucion(plan)
        await pilot.pause(0.2)
        assert e.estado == "error"
        assert e.tarea is None  # nada se lanzó


async def test_refresh_fallido_no_mata_la_tui(entorno: Path, monkeypatch) -> None:
    """P2: un error de red/RPC en el refresh deja la app viva con un aviso."""
    from tg_sentry.tg.client import SesionNoAutorizada

    async def _roto(self):  # type: ignore[no-untyped-def]
        raise SesionNoAutorizada("sesión no autorizada")

    monkeypatch.setattr(SentryApp, "get_client", _roto)
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause(0.3)
        # la app SIGUE viva en el dashboard (antes: crash de toda la TUI)
        assert isinstance(app.screen, DashboardScreen)


async def test_seleccion_sobrevive_al_refresh(entorno: Path) -> None:
    """P3: refrescar la caché NO borra la selección del usuario."""
    app = _FakeApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)  # asienta el filtrado inicial (worker en hilo)
        dashboard = app.screen
        assert isinstance(dashboard, DashboardScreen)
        modelo = dashboard.selection
        bot = next(d for d in dashboard._todos if d.kind is PeerKind.BOT)
        assert modelo.toggle(bot) is True
        assert len(modelo) == 1
        dashboard._cargar()  # lo que hace 'r' tras el refresh
        await pilot.pause(0.3)  # deja correr el worker de filtrado
        assert len(dashboard.selection) == 1
        assert bot.id in dashboard.selection.selected


def test_salida_de_la_app_desconecta_cliente_y_cancela_ejecucion() -> None:
    """2ª auditoría P1: on_unmount + call_later era código muerto (la cola de
    mensajes ya está cerrada al llegar el Unmount) — el cliente Telethon
    JAMÁS se desconectaba al salir y la ejecución moría sin auditar su
    estado. on_exit_app debe: cancelar la tarea, desconectar y parar el
    runtime."""
    import asyncio as _asyncio

    from tg_sentry.tui.app import SentryApp

    class ClienteFalso:
        def __init__(self) -> None:
            self.desconectado = False

        async def disconnect(self) -> None:
            await _asyncio.sleep(0)
            self.desconectado = True

    async def escena() -> None:
        app = SentryApp()
        cliente = ClienteFalso()
        app._client = cliente

        colgado = _asyncio.Event()

        async def tarea_larga() -> None:
            e = app.ejecucion
            assert e is not None
            e.estado = "corriendo"
            try:
                await colgado.wait()
            except _asyncio.CancelledError:
                e.estado = "abortada"
                raise

        ejecucion = Ejecucion(plan=None)  # type: ignore[arg-type]
        app.ejecucion = ejecucion
        ejecucion.tarea = _asyncio.create_task(tarea_larga())
        await _asyncio.sleep(0)  # deja arrancar la tarea

        await app.on_exit_app()

        assert cliente.desconectado, "el cliente NO se desconectó al salir"
        assert ejecucion.tarea is not None and ejecucion.tarea.done()
        assert ejecucion.estado == "abortada"
        assert app._client is None

    _asyncio.run(escena())


async def test_segundo_plan_confirmado_arranca_tras_ejecucion_terminada(
    entorno: Path, app_con_motor_falso
) -> None:
    """P1 3ª auditoría: una ejecución terminada NO bloquea el siguiente plan
    en la misma sesión. ExecuteScreen con un plan NUEVO (recién confirmado)
    arranca su ejecución; la tecla 'e' (mismo plan) solo adjunta."""
    app = app_con_motor_falso()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan_a = build_plan(
            [DialogInfo(id=700, kind=PeerKind.BOT, name="b")],
            PlannerOptions(),
            me_id=42,
        )
        primera = app.iniciar_ejecucion(plan_a)
        await pilot.pause(1.5)
        assert primera.estado == "fin"
        # el usuario confirma un plan B NUEVO (plan_id distinto) → debe arrancar
        plan_b = build_plan(
            [DialogInfo(id=701, kind=PeerKind.BOT, name="b2")],
            PlannerOptions(),
            me_id=42,
        )
        assert plan_b.plan_id != plan_a.plan_id
        app.push_screen(ExecuteScreen(plan_b))
        await pilot.pause(0.1)
        assert app.ejecucion is not primera
        assert app.ejecucion.plan.plan_id == plan_b.plan_id
        await pilot.pause(1.5)
        assert app.ejecucion.estado == "fin"


async def test_iniciar_con_inventario_corrupto_entra_en_error(
    entorno: Path, app_con_motor_falso, monkeypatch
) -> None:
    """P2 3ª auditoría: CacheIncompatible en la re-validación de blindados →
    la ejecución entra en estado 'error' visible, jamás crash del handler."""
    from tg_sentry.inventory import CacheIncompatible

    app = app_con_motor_falso()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=700, kind=PeerKind.BOT, name="b")],
            PlannerOptions(),
            me_id=42,
        )

        def corrupto(*a, **k):  # type: ignore[no-untyped-def]
            raise CacheIncompatible("esquema incompatible")

        monkeypatch.setattr(inventory, "me_id", corrupto)
        e = app.iniciar_ejecucion(plan)
        assert e.estado == "error"
        assert "corrupto" in e.mensaje
