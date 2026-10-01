"""App Textual de tg-sentry. SOLO renderiza y orquesta pantallas.

La lógica vive en el núcleo (inventory/filters/planner/safety/executor):
esta capa no decide nada de seguridad, solo llama y muestra.

Telethon corre en SU PROPIO event loop (hilo dedicado, `TelethonRuntime`):
el loop de Textual instala `eager_task_factory`, que mata al nacer los
loops de envío/recepción de Telethon (hallazgo M4 — ver tg/runtime.py).
Toda operación de red pasa por `telethon_call`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from textual.app import App

from tg_sentry import inventory, runstate
from tg_sentry.audit import AuditLog, StepOutcome
from tg_sentry.config import Config, ConfigError, load_config
from tg_sentry.errors import PlanExpiredError
from tg_sentry.executor import (
    OWN_MESSAGE_STEPS,
    EngineLimits,
    ExecutionEngine,
    RunResult,
)
from tg_sentry.inventory import CacheIncompatible
from tg_sentry.limits import LimitsPolicyError, validate_policy
from tg_sentry.planner import ActionPlan, StepKind, blindados_en_plan
from tg_sentry.runstate import CompletedStep, load_completed, save, step_key
from tg_sentry.tg.runtime import BridgedGateway, TelethonRuntime
from tg_sentry.tui.base import BaseScreen
from tg_sentry.tui.ejecucion import Ejecucion

logger = logging.getLogger(__name__)


class SentryApp(App[None]):
    TITLE = "tg-sentry"
    SUB_TITLE = "monitor y limpieza de Telegram — seguro por diseño"

    CSS = """
    Screen { layout: vertical; }
    #dash { height: 1fr; }
    #panel-filtros { width: 36; height: 1fr; padding: 0 1; }
    #panel-tabla { width: 1fr; height: 1fr; }
    #tabla { height: 1fr; }
    #login-caja { height: auto; margin: 2 4; padding: 1 2; }
    #plan-h { height: 1fr; }
    #plan-opciones { width: 46; padding: 0 1; }
    #plan-tabla { width: 1fr; height: 1fr; }
    #plan-excluidos { height: auto; max-height: 8; }
    #confirm-caja { height: 1fr; padding: 0 2; }
    #aviso { padding: 1 2; }
    #confirm-acciones { height: 3; align: center middle; }
    #confirm-acciones Input { width: 1fr; }
    #confirm-acciones Button { margin-left: 1; }
    #ejecuta-caja { height: 1fr; }
    #estado { height: auto; padding: 0 1; }
    #eta { height: auto; padding: 0 1; }
    #barra { margin: 0 1; }
    #log { height: 1fr; border: round $primary; margin: 0 1; }
    #ejecuta-caja > Button { margin: 0 1; }
    #ejecuta-botones { height: 3; }
    #results-caja { height: 1fr; padding: 0 1; }
    #results-resumen { height: auto; padding: 1 1; }
    #results-agrupado { height: auto; max-height: 10; padding: 0 1; }
    #results-caja DataTable { height: 1fr; }
    #audit-caja { height: 1fr; }
    #audit-filtros { height: 3; padding: 0 1; }
    #audit-filtros Select { width: 34; }
    #audit-filtros Input { width: 1fr; }
    #a-resumen { height: auto; padding: 0 1; }
    #audit-caja DataTable { height: 1fr; }
    #settings-caja { height: 1fr; padding: 1 2; }
    #settings-caja Input { max-width: 70; }
    #s-limits { margin-top: 1; padding: 1 1; }
    #settings-botones { height: 3; margin-top: 1; }
    """

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self._config: Config | None = config
        self._client: object | None = None
        self._runtime = TelethonRuntime()
        self.ejecucion: Ejecucion | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config()
        return self._config

    async def telethon_call(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Ejecuta una coroutine de Telethon en su loop dedicado."""
        return await self._runtime.call(coro)

    @property
    def runtime(self) -> TelethonRuntime:
        return self._runtime

    async def get_client(self) -> object:
        """Cliente Telethon conectado (perezoso, reutilizado entre pantallas).

        JAMÁS interactúa: si la sesión no está autorizada lanza
        SesionNoAutorizada (el camino es la pantalla login; connect_and_login
        usa prompts bloqueantes que dentro de la TUI congelan la UI —
        hallazgo auditoría 2026-09).
        """
        if self._client is None:
            from tg_sentry.tg.client import connect_cliente_autorizado

            telegram = self.config.telegram
            self._client = await self.telethon_call(
                connect_cliente_autorizado(
                    telegram.api_id,
                    telegram.api_hash,
                    telegram.session or None,
                )
            )
        return self._client

    def set_client(self, client: object) -> None:
        """Registra un cliente ya conectado (p. ej. desde la pantalla login)."""
        self._client = client

    def after_login(self) -> None:
        """Enrutamiento tras login exitoso."""
        from tg_sentry.tui.screens.dashboard import DashboardScreen

        self.switch_screen(DashboardScreen())

    def get_default_screen(self) -> BaseScreen:
        """Pantalla inicial correcta de nacimiento (sin switches sobre la pila).

        Una config corrupta NO abre el dashboard: ErrorScreen con el mensaje
        accionable (antes el ConfigError reventaba en on_mount con la pantalla
        de crash de Textual — auditoría 2026-09).
        """
        import tg_sentry.paths as paths

        if paths.session_path().exists():
            try:
                _ = self.config  # fuerza la carga aquí, no en el dashboard
            except ConfigError as exc:
                from tg_sentry.tui.screens.error import ErrorScreen

                return ErrorScreen(f"configuración inválida:\n{exc}")
            from tg_sentry.tui.screens.dashboard import DashboardScreen

            return DashboardScreen()
        from tg_sentry.tui.screens.login import LoginScreen

        return LoginScreen()

    async def close_client(self) -> None:
        if self._client is not None:
            cliente, self._client = self._client, None
            with contextlib.suppress(RuntimeError):  # runtime ya cerrándose
                await self.telethon_call(cliente.disconnect())  # type: ignore[attr-defined]

    async def on_exit_app(self) -> None:
        """Cierre ORDENADO al salir (2ª auditoría, P1): `on_unmount` +
        `call_later` era código muerto — la cola de mensajes de la App ya está
        cerrada cuando corre el Unmount, así que `_shutdown_client` jamás se
        ejecutaba y el cliente Telethon NUNCA se desconectaba (sesión SQLite
        sin cierre limpio; y el runtime paraba con la ejecución a mitad de
        paso, sin dejar auditado el camino). Orden: cancelar ejecución (su
        handler deja estado + auditoría), desconectar cliente, parar runtime."""
        e = self.ejecucion
        if e is not None and e.tarea is not None and not e.tarea.done():
            e.tarea.cancel()
            # CancelledError es BaseException: await al shield del task
            # cancelado la relanza aquí (2ª auditoría)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(e.tarea), timeout=5)
        cliente, self._client = self._client, None
        if cliente is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.telethon_call(cliente.disconnect()),  # type: ignore[attr-defined]
                    timeout=5,
                )
        self._runtime.shutdown()

    # -- ejecución a nivel de app ------------------------------------------
    # La ejecución NO puede vivir en una Screen: Textual cancela los workers
    # de una pantalla al desmontarla y "volver al dashboard" mataba la cola
    # en silencio (hallazgo M6.1). La pantalla es solo una vista adjuntable.

    def iniciar_ejecucion(self, plan: ActionPlan) -> Ejecucion:
        """Inicia la ejecución del plan (1 a la vez) o devuelve la activa."""
        if self.ejecucion is not None and self.ejecucion.activa:
            return self.ejecucion
        ejecucion = Ejecucion(plan=plan)
        # Defensa en profundidad: el plan se re-valida contra las exclusiones
        # duras ANTES de tocar el motor (auditoría 2026-09). Un plan con un
        # blindado dentro NO arranca, ni por accidente ni por edición manual.
        try:
            me = inventory.me_id()
            blindados = (
                []
                if me is None
                else blindados_en_plan(
                    plan,
                    me_id=me,
                    whitelist=self.config.safety.whitelist,
                    contact_ids=inventory.contact_ids(),
                    protect_contacts=self.config.safety.protect_contacts,
                )
            )
        except CacheIncompatible:
            # inventario corrupto en este instante: la ejecución entra en
            # error visible en vez de reventar el handler (3ª auditoría)
            ejecucion.estado = "error"
            ejecucion.mensaje = "inventario corrupto: refresca desde el dashboard"
            ejecucion.loguear(ejecucion.mensaje)
            self.ejecucion = ejecucion
            return ejecucion
        except ValueError as exc:
            # whitelist no parseable (defensa en profundidad, 4ª auditoría):
            # la re-validación de blindados NO puede reventar el arranque
            ejecucion.estado = "error"
            ejecucion.mensaje = f"config inválida: {exc}"
            ejecucion.loguear(ejecucion.mensaje)
            self.ejecucion = ejecucion
            return ejecucion
        if me is None:
            ejecucion.estado = "error"
            ejecucion.mensaje = "sin identidad (me_id): refresca el inventario"
            ejecucion.loguear(ejecucion.mensaje)
            self.ejecucion = ejecucion
            return ejecucion
        if blindados:
            detalle = "; ".join(f"{pid}: {r}" for pid, r in blindados[:5])
            ejecucion.estado = "error"
            ejecucion.mensaje = "plan con peers blindados: NO se ejecuta"
            ejecucion.loguear("BLINDADOS en el plan (regenera desde el dashboard):")
            ejecucion.loguear(detalle)
            logger.error(
                "plan %s con peers blindados: %s", plan.plan_id, blindados
            )
            self.ejecucion = ejecucion
            return ejecucion
        ejecucion.tarea = asyncio.create_task(self._correr_ejecucion(ejecucion))
        self.ejecucion = ejecucion
        return ejecucion

    async def _correr_ejecucion(self, e: Ejecucion) -> None:
        config = self.config
        try:
            # La TUI NO puede pedir --ack-over-limit: si los límites del TOML
            # están subidos, la ejecución se niega (mismo gate que el CLI —
            # auditoría 2026-09: la TUI se saltaba validate_policy).
            validate_policy(config.limits, ack_over_limit=False)
            completados: set[CompletedStep] = load_completed(e.plan)
            if completados:
                e.loguear(f"reanudación: {len(completados)} pasos ya completados")

            e.total = sum(
                1
                for peer in e.plan.peers
                for step in peer.steps
                if (peer.dialog.id, step.kind.value) not in completados
            )
            ritmo = config.limits.writes_per_min
            e.mensaje = f"ejecutando: {e.total} pasos pendientes · ritmo {ritmo}/min"
            e.loguear(f"plan {e.plan.plan_id} — {len(e.plan.peers)} peers")
            e.estado = "corriendo"
            # para el ETA compuesto: los cooldowns de borrado van ADEMÁS del bucket
            e.pasos_borrado_pendientes = sum(
                1
                for peer in e.plan.peers
                for step in peer.steps
                if step.kind in OWN_MESSAGE_STEPS
                and (peer.dialog.id, step.kind.value) not in completados
            )

            if e.total == 0:
                e.estado = "fin"
                e.mensaje = "nada pendiente: el plan ya está ejecutado"
                e.loguear(e.mensaje)
                return

            client = await self.get_client()
            motor = ExecutionEngine(
                BridgedGateway(self._runtime, client),
                EngineLimits(
                    writes_per_min=config.limits.writes_per_min,
                    leave_per_hour=config.limits.leave_groups_per_hour,
                    block_per_hour=config.limits.block_per_hour,
                    delete_messages_per_hour=config.limits.delete_messages_per_hour,
                    floodwait_pause_threshold_s=config.limits.floodwait_pause_threshold_s,
                    floodwait_pattern_window_s=config.limits.floodwait_pattern_window_s,
                    own_messages_cooldown_s=config.limits.own_messages_cooldown_s,
                ),
                AuditLog(
                    rotate_bytes=config.audit.rotate_bytes,
                    keep_days=config.audit.keep_days,
                ),
                sleep=self._sleep_de_ejecucion(e),
                sleep_budget=self._sleep_presupuesto(e),
            )
            def tras_paso(peer_id: int, paso: StepKind, resultado: StepOutcome) -> None:
                # reutiliza el set cargado al inicio (guarda tras CADA paso,
                # sin releer el JSON del disco en cada uno — O(n²) evitado)
                completados.add(step_key(peer_id, paso))
                save(e.plan, completados)
                e.hechos += 1
                if paso in OWN_MESSAGE_STEPS:
                    e.pasos_borrado_pendientes -= 1
                e.loguear(f"peer {peer_id} · {paso.value} → {resultado.value}")

            resultado: RunResult | None = None
            # lock por plan: el CLI apply y la TUI no pueden correr el MISMO
            # plan en dos procesos (auditoría 2026-09)
            with runstate.ejecucion_exclusiva(e.plan) as exclusivo:
                if not exclusivo:
                    e.estado = "error"
                    e.mensaje = "otro proceso está ejecutando este plan"
                    e.loguear(
                        "hay una ejecución ACTIVA de este plan (¿tg-sentry apply "
                        "en otra terminal?): esta no arranca"
                    )
                    return
                resultado = await motor.run(
                    e.plan, completed=completados, after_step=tras_paso
                )
            assert resultado is not None
            e.resultado = resultado

            partes = [f"{resultado.done} done"]
            partes.extend(f"{n} {k}" for k, n in sorted(resultado.skipped.items()))
            partes.append(f"{resultado.failed} failed")
            resumen = " · ".join(partes)
            e.loguear(f"Resultado: {resumen}")
            if resultado.aborted:
                e.estado = "abortada"
                e.loguear(f"⚠ COLA ABORTADA: {resultado.aborted}")
                e.loguear("lo pendiente queda reanudable: reintenta si el plan no venció")
            else:
                e.estado = "fin"
                e.loguear("ver detalles con: tg-sentry audit")
            e.mensaje = f"fin: {resumen}"
        except PlanExpiredError:
            e.estado = "error"
            e.mensaje = "el plan EXPIRÓ (TTL)"
            e.loguear(
                "el plan superó su TTL de re-validación: genera un plan nuevo "
                "desde el dashboard"
            )
        except LimitsPolicyError as exc:
            e.estado = "error"
            e.mensaje = f"límites no permitidos: {exc}"
            e.loguear(
                "los límites del TOML están SUBIDOS respecto al default: la TUI "
                "no puede reconocerlos (solo el CLI con --ack-over-limit). "
                "Bájalos en el TOML o usa el CLI."
            )
        except asyncio.CancelledError:
            # solo al cerrar la app completa (el desmontaje de la pantalla ya
            # NO cancela esta tarea): queda auditado y reanudable
            e.estado = "abortada"
            e.mensaje = "ejecución interrumpida al cerrar tg-sentry"
            raise
        except Exception as exc:
            e.estado = "error"
            e.mensaje = f"error inesperado: {type(exc).__name__}"
            e.loguear(f"error: {exc}")
            e.loguear("los pasos ya ejecutados constan en la auditoría")
            logger.exception("error en ejecución de plan %s", e.plan.plan_id)

    def _sleep_presupuesto(self, e: Ejecucion) -> Callable[[float], Awaitable[None]]:
        """Espera de presupuesto anti-baneo (50/h · 200/d) con aviso visible."""
        from tg_sentry.limits import MAX_DESTRUCTIVE_PER_DAY, MAX_DESTRUCTIVE_PER_HOUR

        async def dormir(segundos: float) -> None:
            espera = f"{int(segundos // 60)} min" if segundos >= 180 else f"{int(segundos)}s"
            e.mensaje = (
                f"presupuesto anti-baneo ({MAX_DESTRUCTIVE_PER_HOUR}/h · "
                f"{MAX_DESTRUCTIVE_PER_DAY}/d) lleno: esperando {espera} a que la "
                "ventana libere — la cola SIGUE sola (puedes minimizar)"
            )
            e.loguear(
                f"espera de presupuesto {MAX_DESTRUCTIVE_PER_HOUR}/h·"
                f"{MAX_DESTRUCTIVE_PER_DAY}/d: {int(segundos)}s (ventana rodante)"
            )
            from datetime import UTC, datetime, timedelta

            e.cooldown_hasta = datetime.now(UTC) + timedelta(seconds=segundos)
            await asyncio.sleep(segundos)
            e.cooldown_hasta = None
            e.mensaje = ""

        return dormir

    def _sleep_de_ejecucion(self, e: Ejecucion) -> Callable[[float], Awaitable[None]]:
        """Sleep que registra esperas largas en el estado visible.

        El motor usa este mismo canal para cooldown de borrado Y para
        FloodWait/backoff: la etiqueta es honesta sobre ambas causas
        (auditoría 2026-09: un FloodWait rotulado "cooldown" engaña).
        """

        async def dormir(segundos: float) -> None:
            if segundos >= 60:
                espera = (
                    f"{int(segundos)}s"
                    if segundos < 180
                    else f"{int(segundos // 60)} min"
                )
                e.mensaje = (
                    f"espera anti-baneo: {espera} "
                    "(cooldown de borrado o FloodWait — puedes minimizar la pantalla)"
                )
                e.loguear(f"esperando {int(segundos)}s (espera anti-baneo)")
                from datetime import UTC, datetime, timedelta

                e.cooldown_hasta = datetime.now(UTC) + timedelta(seconds=segundos)
            await asyncio.sleep(segundos)
            e.cooldown_hasta = None
            e.mensaje = ""

        return dormir
