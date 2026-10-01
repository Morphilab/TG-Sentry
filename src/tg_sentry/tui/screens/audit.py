"""Auditoría desde la TUI: revisar, filtrar y (M6) exportar.

Fuente de verdad: AuditLog.read() (activo + rotados). Filtros por
resultado y texto libre sobre peer/paso/razón. Los estados son
contractuales y se muestran con su nombre exacto.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from tg_sentry.audit import AuditLog, StepOutcome
from tg_sentry.tui.base import BaseScreen


class AuditScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "volver", "volver al dashboard"),
        ("f", "foco_filtro", "ir al filtro"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="audit-caja"):
            with Horizontal(id="audit-filtros"):
                yield Select(
                    [
                        ("todos los resultados", ""),
                        *((o.value, o.value) for o in StepOutcome),
                    ],
                    value="",
                    id="a-outcome",
                    allow_blank=False,
                )
                yield Input(placeholder="filtrar por peer/paso/razón…", id="a-texto")
                yield Button("Export CSV", id="a-export", variant="default")
            yield Static("", id="a-resumen")
            yield DataTable(id="tabla", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tabla", DataTable).add_columns(
            "fecha", "resultado", "peer", "paso", "razón"
        )
        self._refrescar()

    def _registros(self):  # type: ignore[no-untyped-def]

        registros = AuditLog().read()
        outcome = self.query_one("#a-outcome", Select).value
        if outcome:
            registros = [r for r in registros if r.outcome.value == outcome]
        texto = self.query_one("#a-texto", Input).value.strip().lower()
        if texto:
            registros = [
                r
                for r in registros
                if texto in str(r.peer_id)
                or texto in r.step
                or texto in (r.reason or "").lower()
            ]
        return registros

    def _refrescar(self) -> None:
        registros = self._registros()
        resumen: dict[str, int] = {}
        for r in registros:
            resumen[r.outcome.value] = resumen.get(r.outcome.value, 0) + 1
        detalle = " · ".join(f"{n} {k}" for k, n in sorted(resumen.items())) or "sin registros"
        nota = (
            " · tabla: últimos 300" if len(registros) > 300 else ""
        )
        self.query_one("#a-resumen", Static).update(
            f"[b]{len(registros)} registros[/]{nota} — {detalle}"
        )
        tabla = self.query_one("#tabla", DataTable)
        tabla.clear()
        # la tabla muestra los últimos 300 (el resumen cuenta TODO);
        # el re-parseo completo por tecla es deuda conocida (2ª auditoría)
        for r in registros[-300:]:
            color = (
                "green"
                if r.outcome is StepOutcome.DONE
                else "yellow"
                if r.outcome is StepOutcome.FAILED
                else "cyan"
            )
            tabla.add_row(
                r.ts.strftime("%m-%d %H:%M"),
                f"[{color}]{r.outcome.value}[/]",
                str(r.peer_id),
                r.step,
                r.reason or "—",
            )

    def on_input_changed(self, _event: Input.Changed) -> None:
        self._refrescar()

    def on_select_changed(self, _event: Select.Changed) -> None:
        self._refrescar()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "a-export":
            self._exportar()

    def _exportar(self) -> None:
        try:
            from datetime import UTC, datetime

            import tg_sentry.paths as paths
            from tg_sentry.__main__ import _export_audit

            registros = self._registros()
            destino = paths.exports_dir() / f"auditoria-{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
            paths.ensure_private_dir(destino.parent)
            _export_audit(registros, destino, "csv")
        except Exception as exc:
            # un fallo de disco/permisos en un handler NO tumba la TUI
            # (mismo estándar que el refresh del dashboard — 3ª auditoría)
            self.sentry_app.notify(
                f"export falló: {type(exc).__name__}: {exc}", severity="error"
            )
            return
        self.sentry_app.notify(
            f"✓ {len(registros)} registros → {destino} (contiene ids de peers)",
            severity="information",
        )

    def action_foco_filtro(self) -> None:
        self.query_one("#a-texto", Input).focus()

    def action_volver(self) -> None:
        # pop (no switch): esta pantalla se ABRE con push desde el dashboard;
        # pop restaura la instancia viva y conserva su selección (3ª auditoría)
        self.sentry_app.pop_screen()
