"""Dashboard: inventario filtrable + selección masiva.

Solo llama al núcleo (inventory/filters/safety) y renderiza. Los blindados
aparecen con 🔒 y no se pueden seleccionar (decisión de safety, no de la UI).
Atajos: espacio=marcar · a=todo filtrado · i=invertir · n=ninguno ·
p=plan · r=refrescar · f=filtros.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from tg_sentry import inventory
from tg_sentry.filters import FilterError, Filters, apply_filters, safe_compile
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.tui.base import BaseScreen
from tg_sentry.tui.selection import SelectionModel

KIND_LABELS = {
    PeerKind.BOT: "bot",
    PeerKind.USER: "privado",
    PeerKind.GROUP: "grupo",
    PeerKind.CHANNEL: "canal",
    PeerKind.SECRET: "secreto",
    PeerKind.UNKNOWN: "?",
}

logger = logging.getLogger(__name__)


def _dias_desde(dialog: DialogInfo, ahora: datetime | None = None) -> str:
    if dialog.last_message_at is None:
        return "sin mensajes"
    referencia = ahora or datetime.now(UTC)
    dias = int((referencia - dialog.last_message_at).total_seconds() // 86400)
    if dias <= 0:
        return "hoy"
    if dias < 365:
        return f"{dias}d"
    return f"{dias // 365}a{dias % 365}d"


class DashboardScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("space", "marcar", "marcar/desmarcar"),
        ("a", "select_all", "todo lo filtrado"),
        ("i", "invertir", "invertir"),
        ("n", "ninguno", "limpiar selección"),
        ("p", "plan", "construir plan"),
        ("r", "refresh", "refrescar caché"),
        ("f", "filtros", "ir a filtros"),
        ("v", "auditoria", "ver auditoría"),
        ("o", "ajustes", "ajustes y whitelist"),
        ("e", "ejecucion", "ejecución en curso"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._todos: list[DialogInfo] = []
        self._filtrados: list[DialogInfo] = []
        self.selection = SelectionModel()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="dash"):
            with Vertical(id="panel-filtros"):
                yield Label("[b]Filtros[/]")
                yield Checkbox("Bots", True, id="f-bot")
                yield Checkbox("Privados", False, id="f-user")
                yield Checkbox("Grupos", False, id="f-group")
                yield Checkbox("Canales", False, id="f-channel")
                yield Input(placeholder="nombre contiene…", id="f-name")
                yield Input(placeholder="regex de nombre…", id="f-regex")
                yield Input(placeholder="inactivo > N días", id="f-inactive", restrict=r"[0-9]*")
                yield Input(placeholder="unread ≥ N", id="f-unread", restrict=r"[0-9]*")
                yield Select(
                    [
                        ("archivado: cualquiera", "any"),
                        ("archivado: sí", "true"),
                        ("archivado: no", "false"),
                    ],
                    value="any",
                    id="f-archived",
                    allow_blank=False,
                )
                yield Button("Refrescar caché", id="btn-refresh", variant="primary")
                yield Static("", id="resumen")
            with Vertical(id="panel-tabla"):
                yield DataTable(id="tabla", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        tabla = self.query_one("#tabla", DataTable)
        # clave de columna guardada para actualizar SOLO la marca (el cursor
        # de la tabla no se mueve al marcar/desmarcar — bug reportado)
        self._col_sel = tabla.add_column("sel", key="sel")
        tabla.add_columns("tipo", "nombre", "unread", "arch", "actividad")
        self._cargar()
        # el espacio (marcar) es para la TABLA: sin foco aquí, un Checkbox
        # se lo comía y reconstruía la tabla moviendo el cursor al inicio
        tabla.focus()

    def _cargar(self) -> None:
        try:
            self._todos = inventory.load()
            # me_id/contact_ids también leen la DB: un inventario corrupto
            # reventaba AQUÍ (fuera del try) con la pantalla de crash de
            # Textual (2ª auditoría)
            config = self.sentry_app.config
            me = inventory.me_id()
            contactos = inventory.contact_ids()
        except inventory.CacheIncompatible as exc:
            self.sentry_app.notify(f"inventario: {exc}", severity="error")
            return
        # la selección SOBREVIVE al refresh: se re-marcan los ids que siguen
        # existiendo y siguen siendo seleccionables (auditoría 2026-09: antes
        # un refresh borraba la selección del usuario sin avisar)
        previa = self.selection.selected
        try:
            self.selection = SelectionModel(
                me_id=me,
                whitelist=config.safety.whitelist,
                contact_ids=contactos,
                protect_contacts=config.safety.protect_contacts,
            )
        except ValueError as exc:
            # whitelist no parseable (config a mano saltándose el validador):
            # FAIL-CLOSED — sin identidad todo queda 🔒, jamás crash de la app
            # (4ª auditoría)
            self.sentry_app.notify(
                f"whitelist inválida ({exc}): todo queda bloqueado hasta "
                "corregirla en ajustes (o)",
                severity="error",
            )
            self.selection = SelectionModel(me_id=None)
        por_id = {d.id: d for d in self._todos}
        perdidos = 0
        for pid in previa:
            dialog = por_id.get(pid)
            if dialog is None or self.selection.lock_reason(dialog) is not None:
                perdidos += 1
                continue
            self.selection.selected.add(pid)
        if perdidos and previa:
            self.sentry_app.notify(
                f"refresh: {perdidos} seleccionados ya no son seleccionables",
                severity="warning",
            )
        self._aplicar()

    def _filtros_activos(self) -> Filters:
        archivado = self.query_one("#f-archived", Select).value
        kinds = [
            kind
            for kind, widget in [
                (PeerKind.BOT, "#f-bot"),
                (PeerKind.USER, "#f-user"),
                (PeerKind.GROUP, "#f-group"),
                (PeerKind.CHANNEL, "#f-channel"),
            ]
            if self.query_one(widget, Checkbox).value
        ] or None
        inactivo = self.query_one("#f-inactive", Input).value.strip()
        unread = self.query_one("#f-unread", Input).value.strip()
        return Filters(
            kinds=kinds,
            name_contains=self.query_one("#f-name", Input).value.strip() or None,
            name_regex=self.query_one("#f-regex", Input).value.strip() or None,
            inactive_days=int(inactivo) if inactivo else None,
            unread_min=int(unread) if unread else None,
            archived=None if archivado == "any" else archivado == "true",
        )

    def _aplicar(self) -> None:
        # El filtrado corre en un HILO con timeout (anti-ReDoS práctico):
        # un regex catastrófico congela el hilo, no la TUI (auditoría 2026-09).
        if not self.is_mounted:
            return  # nada que filtrar en desmontaje
        # se pasa la FUNCIÓN, no la coroutine: si el worker se descarta antes
        # de arrancar, la coroutine jamás se crea (sin RuntimeWarning de GC —
        # 4ª auditoría)
        self.run_worker(self._aplicar_worker, exclusive=True)  # type: ignore[arg-type]

    async def _aplicar_worker(self) -> None:
        import asyncio

        filtros = self._filtros_activos()
        try:
            if filtros.name_regex:
                safe_compile(filtros.name_regex)
            self._filtrados = await asyncio.wait_for(
                asyncio.to_thread(apply_filters, self._todos, filtros),
                timeout=5.0,
            )
        except FilterError as exc:
            self.sentry_app.notify(f"filtro: {exc}", severity="error")
            self._filtrados = []
        except TimeoutError:
            self.sentry_app.notify(
                "el regex es demasiado costoso: afina el patrón", severity="error"
            )
            self._filtrados = []
        except ValueError as exc:  # ValidationError (p. ej. días fuera de rango)
            self.sentry_app.notify(f"filtro inválido: {exc}", severity="error")
            self._filtrados = []
        self._render_tabla()
        self._render_resumen()

    def _marca_de(self, dialog: DialogInfo) -> str:
        if self.selection.lock_reason(dialog) is not None:
            return "🔒"
        return "✓" if dialog.id in self.selection.selected else "·"

    def _render_tabla(self) -> None:
        """Reconstrucción completa (solo al cambiar filtros): preserva el cursor."""
        tabla = self.query_one("#tabla", DataTable)
        cursor_previo = tabla.cursor_row
        tabla.clear()
        for dialog in self._filtrados:
            tabla.add_row(
                self._marca_de(dialog),
                KIND_LABELS.get(dialog.kind, "?"),
                dialog.display,
                str(dialog.unread),
                "sí" if dialog.archived else "no",
                _dias_desde(dialog),
                key=str(dialog.id),
            )
        if 0 <= cursor_previo < len(self._filtrados):
            tabla.move_cursor(row=cursor_previo)

    def _actualizar_marcas(self) -> None:
        """Actualiza solo la columna de selección: el cursor NO se mueve."""
        tabla = self.query_one("#tabla", DataTable)
        for dialog in self._filtrados:
            tabla.update_cell(
                str(dialog.id), self._col_sel, self._marca_de(dialog), update_width=False
            )

    def _render_resumen(self) -> None:
        conteos: dict[str, int] = {}
        for dialog in self._filtrados:
            etiqueta = KIND_LABELS.get(dialog.kind, "?")
            conteos[etiqueta] = conteos.get(etiqueta, 0) + 1
        detalle = " · ".join(f"{n} {k}" for k, n in sorted(conteos.items())) or "sin resultados"
        self.query_one("#resumen", Static).update(
            f"filtros: {len(self._filtrados)}/{len(self._todos)}\n{detalle}\n"
            f"[b]seleccionados: {len(self.selection)}[/]"
        )

    def _dialogo_en_fila(self) -> DialogInfo | None:
        tabla = self.query_one("#tabla", DataTable)
        if tabla.cursor_row is None or tabla.cursor_row < 0:
            return None
        try:
            fila = self._filtrados[tabla.cursor_row]
        except IndexError:
            return None
        return fila

    # -- acciones de teclado ------------------------------------------------

    def action_marcar(self) -> None:
        dialog = self._dialogo_en_fila()
        if dialog is None:
            return
        if not self.selection.toggle(dialog):
            self.sentry_app.notify(
                f"🔒 blindado: {self.selection.lock_reason(dialog)}", severity="warning"
            )
            return
        self._actualizar_marcas()
        self._render_resumen()

    def action_select_all(self) -> None:
        delta = self.selection.select_all(self._filtrados)
        self._actualizar_marcas()
        self._render_resumen()
        self.sentry_app.notify(f"selección: +{delta} (total {len(self.selection)})")

    def action_invertir(self) -> None:
        delta = self.selection.invert(self._filtrados)
        signo = "+" if delta >= 0 else ""
        self._actualizar_marcas()
        self._render_resumen()
        self.sentry_app.notify(
            f"selección: {signo}{delta} (total {len(self.selection)})"
        )

    def action_ninguno(self) -> None:
        self.selection.clear()
        self._actualizar_marcas()
        self._render_resumen()

    def action_filtros(self) -> None:
        self.query_one("#f-name", Input).focus()

    def action_auditoria(self) -> None:
        from tg_sentry.tui.screens.audit import AuditScreen

        self.sentry_app.push_screen(AuditScreen())

    def action_ejecucion(self) -> None:
        e = self.sentry_app.ejecucion
        if e is None:
            self.sentry_app.notify("no hay ninguna ejecución", severity="warning")
            return
        from tg_sentry.tui.screens.execute import ExecuteScreen

        self.sentry_app.push_screen(ExecuteScreen(e.plan))

    def action_ajustes(self) -> None:
        from tg_sentry.tui.screens.settings import SettingsScreen

        self.sentry_app.push_screen(SettingsScreen())

    def action_plan(self) -> None:
        if not self.selection:
            self.sentry_app.notify("selecciona algo primero (espacio / a)", severity="warning")
            return
        # El plan usa TODA la selección (no solo lo visible): el contador de
        # la UI debe cuadrar con lo que entra al plan (auditoría 2026-09).
        seleccion = self.selection.selected_dialogs(self._todos)
        ocultos = len(seleccion) - len(self.selection.selected_dialogs(self._filtrados))
        if ocultos > 0:
            self.sentry_app.notify(
                f"{ocultos} seleccionados están ocultos por los filtros y ENTRARÁN al plan",
                severity="warning",
            )
        from tg_sentry.tui.screens.plan import PlanScreen

        self.sentry_app.push_screen(PlanScreen(seleccion))

    def action_refresh(self) -> None:
        # función, no coroutine invocada (mismo motivo que _aplicar)
        self.run_worker(self._refresh, exclusive=True)  # type: ignore[arg-type]

    async def _refresh(self) -> None:
        # try/except: un fallo de red/RPC en el refresh NO puede matar la TUI
        # entera (un worker en error cierra la app — Textual exit_on_error;
        # auditoría 2026-09).
        try:
            self.sentry_app.notify("actualizando inventario (solo lectura)…")
            client = await self.sentry_app.get_client()
            stats = await self.sentry_app.telethon_call(inventory.refresh(client))
            self._cargar()
            self.sentry_app.notify(f"inventario: {stats.total} diálogos")
        except Exception as exc:
            logger.warning("refresh falló", exc_info=True)
            self.sentry_app.notify(f"refresh falló: {exc}", severity="error")

    # -- eventos de widgets ---------------------------------------------------

    def on_checkbox_changed(self, _event: Checkbox.Changed) -> None:
        self._aplicar()

    def on_input_changed(self, _event: Input.Changed) -> None:
        self._aplicar()

    def on_select_changed(self, _event: Select.Changed) -> None:
        self._aplicar()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-refresh":
            self.action_refresh()
