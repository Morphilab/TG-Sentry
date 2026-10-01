"""Vista de ejecución: SOLO renderiza el estado de la ejecución de la app.

La ejecución vive en SentryApp.ejecucion (tarea asyncio propia): esta
pantalla se puede abrir/cerrar sin afectarla (hallazgo M6.1 — antes el
worker moría al desmontar y "volver" abortaba en silencio). Se adjunta a
la ejecución existente o la inicia si no hay ninguna. Un set_interval
refresca barra/ETA/log desde el estado compartido.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    ProgressBar,
    RichLog,
    Static,
)

from tg_sentry.planner import ActionPlan
from tg_sentry.tui.base import BaseScreen
from tg_sentry.tui.ejecucion import Ejecucion


class ExecuteScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "volver", "volver (la ejecución SIGUE en segundo plano)"),
    ]

    def __init__(self, plan: ActionPlan) -> None:
        super().__init__()
        self._plan = plan
        self._ejecucion: Ejecucion | None = None
        self._log_leido = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="ejecuta-caja"):
            yield Static("preparando ejecución…", id="estado")
            yield Static("", id="aviso-cooldown")
            yield ProgressBar(id="barra", show_eta=False, show_percentage=True)
            yield Static("", id="eta")
            yield RichLog(id="log", markup=False, wrap=True)
            with Horizontal(id="ejecuta-botones"):
                yield Button(
                    "Resultados", id="btn-resultados", variant="primary", disabled=True
                )
                yield Button(
                    "Volver al dashboard (la ejecución sigue)",
                    id="btn-volver",
                    variant="default",
                )
        yield Footer()

    def on_mount(self) -> None:
        # ADJUNTAR, no relanzar (P1 auditoría 2026-09): la tecla 'e' abre esta
        # pantalla con el plan de la ejecución EXISTENTE — relanzarla aquí
        # reanudaba destructivamente sin CONFIRMAR. El discriminador es el
        # plan_id: mismo plan → adjuntar siempre (activa o terminada); plan
        # DISTINTO solo arranca si no hay ejecución ACTIVA (plan recién
        # confirmado; 3ª auditoría: antes una ejecución terminada bloqueaba
        # para siempre el siguiente plan en la misma sesión).
        actual = self.sentry_app.ejecucion
        if actual is not None and actual.plan.plan_id == self._plan.plan_id:
            self._ejecucion = actual
            if actual.estado in {"abortada", "error", "fin"}:
                self.sentry_app.notify(
                    f"esta ejecución ya terminó ({actual.estado}); "
                    "para reanudar genera un plan nuevo (p)",
                    severity="warning",
                )
        elif actual is not None and actual.activa:
            self._ejecucion = actual
            self.sentry_app.notify(
                "ya hay una ejecución (1 a la vez): se muestra la activa, "
                "de OTRO plan",
                severity="warning",
            )
        else:
            self._ejecucion = self.sentry_app.iniciar_ejecucion(self._plan)
        self.set_interval(0.5, self._refrescar)
        self._refrescar()

    # -- render desde el estado compartido ----------------------------------

    def _refrescar(self) -> None:
        if self._ejecucion is None:  # pragma: no cover — asignado en on_mount
            return
        if not self.is_current:
            # pantalla suspendida bajo otra (switch_screen la deja montada):
            # el interval sigue vivo pero no hay nada que pintar (P3 auditoría)
            return
        e = self._ejecucion

        self.query_one("#estado", Static).update(
            f"[b]{e.estado}[/] — {e.mensaje or f'{e.hechos}/{e.total} pasos'}"
        )

        barra = self.query_one("#barra", ProgressBar)
        barra.total = max(1, e.total)
        barra.update(progress=e.hechos)

        eta = self.query_one("#eta", Static)
        if e.estado == "corriendo" and e.total > 0:
            from tg_sentry.limits import MAX_DESTRUCTIVE_PER_HOUR

            ritmo = self.sentry_app.config.limits.writes_per_min
            pendientes = e.total - e.hechos
            # ritmo efectivo: el tope de sesión (50/h) suele mandar por
            # encima del bucket en trabajos grandes — el ETA no puede mentir
            ritmo_efectivo = min(ritmo, MAX_DESTRUCTIVE_PER_HOUR / 60)
            if ritmo_efectivo < ritmo:
                base = (
                    f"ETA ~{pendientes / ritmo_efectivo / 60:.1f} h "
                    f"(tope de sesión {MAX_DESTRUCTIVE_PER_HOUR}/h manda)"
                )
            else:
                base = f"ETA ~{pendientes / ritmo:.0f} min a {ritmo} ops/min"
            # los cooldowns de borrado van ADEMÁS del bucket: espera actual
            # (cooldown en curso) + rango de los que faltan (piso 60s c/u)
            from datetime import UTC, datetime

            espera_actual = 0.0
            if e.cooldown_hasta is not None:
                espera_actual = max(
                    0.0, (e.cooldown_hasta - datetime.now(UTC)).total_seconds()
                )
            techo = self.sentry_app.config.limits.own_messages_cooldown_s
            cmin = espera_actual + e.pasos_borrado_pendientes * 60
            cmax = espera_actual + e.pasos_borrado_pendientes * techo
            if cmin > 0:
                base += (
                    f" + {cmin / 60:.0f}-{cmax / 60:.0f} min de cooldowns"
                    f" ({e.pasos_borrado_pendientes} pasos de borrado por correr)"
                )
            eta.update(
                f"{base} · {e.hechos}/{e.total}"
                " (la ejecución sigue si sales de aquí)"
            )
        else:
            eta.update("")

        log = self.query_one("#log", RichLog)
        for linea in e.log[self._log_leido :]:
            log.write(linea)
        self._log_leido = len(e.log)

        terminada = e.estado in {"fin", "abortada", "error"}
        self.query_one("#btn-resultados", Button).disabled = not terminada

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-volver":
            self.action_volver()
        elif event.button.id == "btn-resultados":
            from tg_sentry.tui.screens.results import ResultsScreen

            # resultados del plan EJECUTADO (si se adjuntó a la ejecución de
            # otro plan, self._plan y el ejecutado no coinciden — 3ª auditoría)
            ejecucion = self._ejecucion
            self.sentry_app.push_screen(
                ResultsScreen(
                    ejecucion.plan if ejecucion is not None else self._plan,
                    ejecucion.resultado if ejecucion else None,
                )
            )

    def action_volver(self) -> None:
        from tg_sentry.tui.screens.dashboard import DashboardScreen

        self.sentry_app.switch_screen(DashboardScreen())
