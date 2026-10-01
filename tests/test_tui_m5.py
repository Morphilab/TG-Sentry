"""Tests de las pantallas M5 (resultados, auditoría, ajustes) con pilot. Sin red."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_inventory import _to_infos
from tg_sentry import inventory
from tg_sentry.audit import AuditLog, AuditRecord, StepOutcome
from tg_sentry.config import Config, save_config
from tg_sentry.executor import RunResult
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import PlannerOptions, build_plan
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
    sesion = tmp_path / "data" / "tg-sentry" / "tg-sentry.session"
    sesion.parent.mkdir(parents=True, exist_ok=True)
    sesion.write_bytes(b"sqlite-ficticia")
    return tmp_path


def _sembrar_auditoria() -> None:
    audit = AuditLog(rotate_bytes=10**9, keep_days=90)
    ahora = datetime.now(UTC)
    for index, (peer, outcome, razon) in enumerate(
        [
            (100, StepOutcome.DONE, None),
            (200, StepOutcome.FAILED, "admin_required: no puedes"),
            (300, StepOutcome.SKIPPED_UNREACHABLE, "peer_invalid"),
        ]
    ):
        audit.record(
            AuditRecord(
                ts=ahora - timedelta(minutes=index),
                plan_id="p-test",
                peer_id=peer,
                step="delete_chat",
                outcome=outcome,
                reason=razon,
            )
        )


async def test_results_screen_agrupa_y_muestra(entorno: Path) -> None:
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [d for d in _to_infos() if d.kind is PeerKind.BOT],
            PlannerOptions(),
            me_id=42,
        )
        from tg_sentry.tui.screens.results import ResultsScreen

        app.push_screen(ResultsScreen(plan, RunResult(done=2, failed=1)))
        await pilot.pause()
        resumen = str(app.screen.query_one("#results-resumen").render())
        assert "2 done" in resumen and "1 failed" in resumen
        agrupado = str(app.screen.query_one("#results-agrupado").render())
        assert "delete_chat" in agrupado and "block" in agrupado
        assert app.screen.query_one("#tabla").row_count == len(plan.peers) * len(
            plan.peers[0].steps
        )


async def test_audit_screen_filtrable(entorno: Path) -> None:
    _sembrar_auditoria()
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from tg_sentry.tui.screens.audit import AuditScreen

        app.push_screen(AuditScreen())
        await pilot.pause()
        resumen = str(app.screen.query_one("#a-resumen").render())
        assert "3 registros" in resumen
        # filtro por texto → solo el fallo de admin
        app.screen.query_one("#a-texto").value = "admin"
        await pilot.pause()
        resumen = str(app.screen.query_one("#a-resumen").render())
        assert "1 registros" in resumen and "failed" in resumen
        # filtro por resultado → solo skipped_unreachable
        app.screen.query_one("#a-texto").value = ""
        app.screen.query_one("#a-outcome").value = "skipped_unreachable"
        await pilot.pause()
        resumen = str(app.screen.query_one("#a-resumen").render())
        assert "1 registros" in resumen


async def test_settings_guarda_whitelist_y_protecciones(entorno: Path, monkeypatch) -> None:
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from tg_sentry.tui.screens.settings import SettingsScreen

        app.push_screen(SettingsScreen())
        await pilot.pause()
        app.screen.query_one("#s-whitelist").value = "123456789, @Durov"
        app.screen.query_one("#s-recent").value = "15"
        await pilot.pause()
        app.screen.query_one("#btn-guardar").press()
        await pilot.pause()
        config = app.config
        assert config.safety.whitelist == ["123456789", "@Durov"]
        assert config.safety.recent_private_days == 15
        # y quedó persistido en el TOML
        import tg_sentry.paths as paths
        from tg_sentry.config import load_config

        config2 = load_config(paths.config_path())
        assert config2.safety.whitelist == ["123456789", "@Durov"]
        assert config2.safety.recent_private_days == 15


async def test_execute_con_barra_y_resultados(entorno: Path, monkeypatch) -> None:
    from tg_sentry.tui.screens.execute import ExecuteScreen
    from tg_sentry.tui.screens.results import ResultsScreen

    class FakeGateway:
        def __init__(self, runtime, client) -> None:
            pass

        async def run_step(self, step, dialog) -> None:  # type: ignore[no-untyped-def]
            return None

    class FakeRuntime:
        async def call(self, coro):  # type: ignore[no-untyped-def]
            return await coro

    class FakeApp(SentryApp):
        async def get_client(self):  # type: ignore[no-untyped-def]
            return object()

        @property
        def runtime(self):  # type: ignore[no-untyped-def]
            return FakeRuntime()

    monkeypatch.setattr("tg_sentry.tui.app.BridgedGateway", FakeGateway)
    app = FakeApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        plan = build_plan(
            [DialogInfo(id=100, kind=PeerKind.BOT, name="b")], PlannerOptions(), me_id=42
        )
        app.push_screen(ExecuteScreen(plan))
        for _ in range(20):
            await pilot.pause(0.2)
            texto = str(app.screen.query_one("#estado").render())
            if "fin:" in texto:
                break
        assert "fin:" in str(app.screen.query_one("#estado").render())
        barra = app.screen.query_one("#barra")
        assert barra.percentage == 1.0  # Textual: 0..1
        boton = app.screen.query_one("#btn-resultados")
        assert boton.disabled is False
        boton.press()
        await pilot.pause()
        assert isinstance(app.screen, ResultsScreen)


async def test_settings_rechaza_whitelist_malformada_sin_guardar(entorno: Path) -> None:
    """4ª auditoría: guardar '--77' rompería el dashboard/plan al recargar
    (parse_whitelist lanza ValueError sin capturar). Se rechaza EN EL
    GUARDADO con mensaje visible: la config no cambia y no se navega."""
    app = SentryApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from tg_sentry.config import load_config
        from tg_sentry.tui.screens.settings import SettingsScreen

        app.push_screen(SettingsScreen())
        await pilot.pause()
        app.screen.query_one("#s-whitelist").value = "--77"
        await pilot.pause()
        app.screen.query_one("#btn-guardar").press()
        await pilot.pause()
        # no guardó ni navegó: mensaje de error visible en pantalla
        assert isinstance(app.screen, SettingsScreen)
        assert "no guardado" in str(app.screen.query_one("#s-estado").render())
        assert "--77" in str(app.screen.query_one("#s-estado").render())
        import tg_sentry.paths as paths

        assert load_config(paths.config_path()).safety.whitelist == []
