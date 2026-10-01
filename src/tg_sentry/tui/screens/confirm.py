"""Barrera de confirmación (la última antes de lo irreversible).

Elementos obligatorios por diseño: contador de destructivas por tipo,
lista de blindados excluidos, aviso PROMINENTE de riesgo de suspensión,
botón cancelar visible, y teclear CONFIRMAR exactamente.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Static

from tg_sentry.planner import ActionPlan
from tg_sentry.tui.base import BaseScreen
from tg_sentry.tui.screens.dashboard import KIND_LABELS

AVISO = """[b red on black]⚠  ESTA HERRAMIENTA PUEDE PROVOCAR LA SUSPENSIÓN DE TU CUENTA ⚠[/]

Los borrados en Telegram son [b]IRREVERSIBLES[/]. tg-sentry aplica límites
conservadores y respeta FloodWait, pero [b]ningún software elimina el riesgo
de baneo[/]. Úsala bajo tu responsabilidad.

Tiempo restante del plan: [b]{ttl}[/]. Si llega a 0 hay que generarlo de
nuevo; lo ejecutado consta en la auditoría pero NO se puede deshacer."""


class ConfirmScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "cancelar", "cancelar (nada se ejecuta)"),
    ]
    AUTO_FOCUS = "#palabra"

    def __init__(self, plan: ActionPlan) -> None:
        super().__init__()
        self._plan = plan

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        # VerticalScroll: si el contenido desborda, se scrollea — el campo y
        # los botones siempre quedan alcanzables (bug reportado por el usuario)
        with VerticalScroll(id="confirm-caja"):
            yield Static(
                AVISO.format(ttl=self._restante_legible()), id="aviso"
            )
            conteo = " · ".join(
                f"{n} {k}" for k, n in sorted(self._plan.counts_by_step().items())
            )
            yield Static(
                f"[b]{len(self._plan.peers)} peers · "
                f"{self._plan.destructive_count()} operaciones destructivas[/]\n{conteo}",
                id="contador",
            )
            por_tipo: dict[str, int] = {}
            for peer in self._plan.peers:
                etiqueta = KIND_LABELS.get(peer.dialog.kind, "?")
                por_tipo[etiqueta] = por_tipo.get(etiqueta, 0) + len(peer.steps)
            tipos = " · ".join(f"{n} {k}" for k, n in sorted(por_tipo.items()))
            yield Static(f"pasos por tipo: {tipos}", id="tipos")
            excluidos = self._plan.excluded[:8]
            lineas = "\n".join(f"— {pid}: {razon}" for pid, razon in excluidos)
            resto = (
                f"\n… y {len(self._plan.excluded) - 8} más (🔒 blindados, fuera del plan)"
                if len(self._plan.excluded) > 8
                else ""
            )
            yield Static(
                f"[b]🔒 NO se va a tocar ({len(self._plan.excluded)} blindados):[/]\n"
                f"{lineas or '—'}{resto}",
                id="excluidos",
            )
            yield Label("Teclea CONFIRMAR y pulsa Enter (o botón «Ejecutar»):")
            with Horizontal(id="confirm-acciones"):
                yield Input(placeholder="CONFIRMAR", id="palabra")
                yield Button("CANCELAR — nada se ejecuta", variant="error", id="btn-cancelar")
                yield Button("Ejecutar", variant="warning", id="btn-ejecutar", disabled=True)
        yield Footer()

    def action_cancelar(self) -> None:
        self.sentry_app.notify("cancelado: nada se ejecutó")
        self.sentry_app.switch_screen(_dashboard())

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palabra":
            self.query_one("#btn-ejecutar", Button).disabled = event.value != "CONFIRMAR"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter tras teclear CONFIRMAR = ejecutar (equivalente al botón)
        if event.input.id == "palabra" and event.value == "CONFIRMAR":
            self._ejecutar()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancelar":
            self.sentry_app.notify("cancelado: nada se ejecutó")
            self.sentry_app.switch_screen(_dashboard())
        elif event.button.id == "btn-ejecutar":
            self._ejecutar()

    def _restante_legible(self) -> str:
        """TTL RESTANTE del plan (no el de config): quedarse mirando esta
        pantalla consume el TTL y antes se descubría solo tras CONFIRMAR
        (2ª auditoría). restante_ttl() acota a 0: 0 = expirado."""
        restante = self._plan.restante_ttl()
        if restante.total_seconds() <= 0:
            return "expirado"
        minutos = int(restante.total_seconds() // 60)
        return f"{minutos} min" if minutos >= 1 else f"{int(restante.total_seconds())}s"

    def _ejecutar(self) -> None:
        from tg_sentry.tui.screens.execute import ExecuteScreen

        if self._plan.is_expired():
            self.sentry_app.notify(
                "el plan EXPIRÓ mientras confirmabas: regenera desde el plan",
                severity="error",
            )
            return
        self.sentry_app.switch_screen(ExecuteScreen(self._plan))


def _dashboard():  # type: ignore[no-untyped-def]
    from tg_sentry.tui.screens.dashboard import DashboardScreen

    return DashboardScreen()
