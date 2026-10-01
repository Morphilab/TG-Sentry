"""Resultados de la ejecución: resumen del RunResult + detalle peer x paso.

El desglose agrupado es por PASO x tipo (la razón por ítem vive en la
auditoría — tecla 'v' o tg-sentry audit). Los estados skip son
contractuales y NO se colapsan: skipped_ok es idempotencia real;
skipped_unreachable NO es éxito; skipped_external lo cambió un tercero.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Static

from tg_sentry.audit import StepOutcome
from tg_sentry.executor import RunResult
from tg_sentry.planner import ActionPlan
from tg_sentry.tui.base import BaseScreen
from tg_sentry.tui.screens.dashboard import KIND_LABELS

_TITULOS = {
    StepOutcome.DONE: "done",
    StepOutcome.SKIPPED_OK: "skipped_ok (ya hecho por nosotros)",
    StepOutcome.SKIPPED_UNREACHABLE: "skipped_unreachable (nunca accesible)",
    StepOutcome.SKIPPED_EXTERNAL: "skipped_external (lo cambió un tercero)",
    StepOutcome.FAILED: "failed",
}


class ResultsScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "volver", "volver al dashboard"),
    ]

    def __init__(self, plan: ActionPlan, resultado: RunResult | None) -> None:
        super().__init__()
        self._plan = plan
        self._resultado = resultado

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="results-caja"):
            yield Static("", id="results-resumen")
            yield Static("", id="results-agrupado")
            yield DataTable(id="tabla", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tabla", DataTable).add_columns(
            "peer", "tipo", "paso", "resultado", "razón"
        )
        self._refrescar()

    def _refrescar(self) -> None:
        plan = self._plan
        resultado = self._resultado
        if resultado is None:
            self.query_one("#results-resumen", Static).update(
                "[b]sin resultado registrado[/] (la ejecución no terminó aquí)"
            )
        else:
            partes = [f"{resultado.done} done"]
            partes.extend(f"{n} {k}" for k, n in sorted(resultado.skipped.items()))
            partes.append(f"{resultado.failed} failed")
            if resultado.messages_deleted:
                partes.append(f"{resultado.messages_deleted} mensajes borrados")
            aborto = (
                f"\n[b red]COLA ABORTADA: {resultado.aborted}[/]"
                if resultado.aborted
                else ""
            )
            self.query_one("#results-resumen", Static).update(
                f"[b]plan {plan.plan_id}[/]\n{' · '.join(partes)}{aborto}"
            )

        # agrupación por resultado x tipo de peer x razón (a partir del plan)
        por_grupo: dict[tuple[str, str, str], list[int]] = {}
        for peer in plan.peers:
            for paso in peer.steps:
                clave = (
                    paso.kind.value,
                    KIND_LABELS.get(peer.dialog.kind, "?"),
                    "—",
                )
                por_grupo.setdefault(clave, []).append(peer.dialog.id)
        lineas = [
            f"{len(ids)} x {paso} sobre {tipo} ({razon})"
            for (paso, tipo, razon), ids in sorted(por_grupo.items())
        ]
        self.query_one("#results-agrupado", Static).update(
            "[b]planificado:[/]\n" + "\n".join(lineas)
        )

        tabla = self.query_one("#tabla", DataTable)
        for indice, peer in enumerate(plan.peers):
            for paso_orden, paso in enumerate(peer.steps):
                tabla.add_row(
                    peer.dialog.display,
                    KIND_LABELS.get(peer.dialog.kind, "?"),
                    paso.kind.value,
                    "(ver audit)",  # el resultado real vive en la auditoría
                    "",
                    key=f"{indice}:{paso_orden}",
                )


    def action_volver(self) -> None:
        self.sentry_app.switch_screen(_dashboard())


def _dashboard():  # type: ignore[no-untyped-def]
    from tg_sentry.tui.screens.dashboard import DashboardScreen

    return DashboardScreen()
