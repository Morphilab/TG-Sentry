"""Pantalla de plan (dry-run): pasos por peer, avisos, excluidos, opciones.

NADA se ejecuta aquí. Las opciones del planner se pueden ajustar y el plan
se regenera en vivo (con TTL nuevo). 'Ir a confirmación' lleva a la barrera.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Label,
    Static,
)

from tg_sentry import inventory
from tg_sentry.config import Config
from tg_sentry.inventory import CacheIncompatible
from tg_sentry.models import DialogInfo
from tg_sentry.planner import ActionPlan, PlannerOptions, build_plan
from tg_sentry.tui.base import BaseScreen
from tg_sentry.tui.screens.confirm import ConfirmScreen
from tg_sentry.tui.screens.dashboard import KIND_LABELS


class PlanScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "volver", "volver al dashboard"),
        ("c", "confirmar", "ir a confirmación"),
    ]

    def __init__(self, seleccion: list[DialogInfo]) -> None:
        super().__init__()
        self._seleccion = seleccion
        self._plan: ActionPlan | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="plan-h"):
            with Vertical(id="plan-opciones"):
                yield Label("[b]Opciones del plan[/]")
                yield Checkbox("Bloquear bots", True, id="o-block-bots")
                yield Checkbox("Bloquear en privados", False, id="o-block-private")
                yield Checkbox(
                    "Privados: borrar también del otro lado (revoke)", False, id="o-revoke"
                )
                yield Checkbox(
                    "Vaciar historial antes de salir (NO disponible)",
                    False,
                    id="o-clear",
                    disabled=True,
                )
                yield Static("[b]Mensajes propios[/]")
                yield Checkbox(
                    "Borrar mis mensajes SOLO para mí (sin límite de edad)",
                    False,
                    id="o-delete-own-me",
                )
                yield Checkbox(
                    "…para todos donde se pueda (≤48h; luego degrada a solo-ti)",
                    False,
                    id="o-delete-own-all",
                )
                yield Checkbox(
                    "Salir del chat (desmarcado = solo limpia mensajes)",
                    True,
                    id="o-leave",
                )
                yield Static("", id="plan-contadores")
                yield Button("Ir a confirmación →", variant="warning", id="btn-confirmar")
            with Vertical(id="plan-tabla"):
                yield DataTable(id="tabla", cursor_type="row", zebra_stripes=True)
                yield Static("", id="plan-excluidos")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tabla", DataTable).add_columns(
            "tipo", "nombre", "pasos", "avisos"
        )
        self._regenerar()

    def _opciones(self) -> PlannerOptions:
        own_me = self.query_one("#o-delete-own-me", Checkbox).value
        own_all = self.query_one("#o-delete-own-all", Checkbox).value
        delete_own: Literal[None, "all", "me"] = (
            "all" if own_all else "me" if own_me else None
        )
        if own_all and own_me:
            own_all = False  # "me" gana: es el modo seguro, la UI lo aclara
            delete_own = "me"
            self.query_one("#o-delete-own-all", Checkbox).value = False
        # frescura REAL de la caché (2ª auditoría: el default True mentía —
        # un plan sobre un inventario de hace semanas llegaba a confirmar sin
        # ninguna señal de staleness)
        caducada = inventory.is_stale(self.sentry_app.config.cache.ttl_min)
        return PlannerOptions(
            block_bots=self.query_one("#o-block-bots", Checkbox).value,
            block_private=self.query_one("#o-block-private", Checkbox).value,
            revoke_private=self.query_one("#o-revoke", Checkbox).value,
            clear_history_before_leave=False,  # UnsupportedStep hasta piloto
            delete_own=delete_own,
            leave=self.query_one("#o-leave", Checkbox).value,
            ttl_min=self.sentry_app.config.plan.ttl_min,
            based_on_cache_fresh=not caducada,
        )

    def _regenerar(self) -> None:
        config = self.sentry_app.config
        try:
            self._regenerar_inner(config)
        except CacheIncompatible as exc:
            # inventario corrupto: visible y sin plan, igual que el dashboard
            # (3ª auditoría: antes traceback/crash screen)
            self._plan = None
            self.query_one("#tabla", DataTable).clear()
            self.query_one("#plan-contadores", Static).update(
                f"[b red]INVENTARIO CORRUPTO:[/] {exc}\nRefresca el inventario (r)."
            )
            self.query_one("#plan-excluidos", Static).update("")

    def _regenerar_inner(self, config: Config) -> None:
        me = inventory.me_id()
        if me is None:
            # Fail-closed: sin identidad propia no hay plan (auditoría 2026-09).
            self._plan = None
            self.query_one("#tabla", DataTable).clear()
            self.query_one("#plan-contadores", Static).update(
                "[b]SIN IDENTIDAD (me_id): refresca el inventario (r) y vuelve "
                "al plan.[/]"
            )
            self.query_one("#plan-excluidos", Static).update("")
            return
        try:
            self._plan = build_plan(
                self._seleccion,
                self._opciones(),
                me_id=me,
                whitelist=config.safety.whitelist,
                contact_ids=inventory.contact_ids(),
                protect_contacts=config.safety.protect_contacts,
                recent_private_days=config.safety.recent_private_days,
            )
        except ValueError as exc:
            # whitelist no parseable (defensa en profundidad: el validador de
            # config ya la rechaza al cargar): sin plan, visible, sin crash
            # (4ª auditoría)
            self._plan = None
            self.query_one("#tabla", DataTable).clear()
            self.query_one("#plan-contadores", Static).update(
                f"[b red]WHITELIST/CONFIG INVÁLIDA:[/] {exc}\n"
                "Corrígela en ajustes (o) y vuelve al plan."
            )
            self.query_one("#plan-excluidos", Static).update("")
            return
        tabla = self.query_one("#tabla", DataTable)
        tabla.clear()
        for peer in self._plan.peers:
            pasos = " → ".join(s.kind.value for s in peer.steps)
            avisos = "; ".join(peer.warnings) or "—"
            tabla.add_row(
                KIND_LABELS.get(peer.dialog.kind, "?"), peer.dialog.display, pasos, avisos
            )
        conteo = " · ".join(f"{n} {k}" for k, n in sorted(self._plan.counts_by_step().items()))
        edad = inventory.cache_age()
        aviso_caducada = ""
        if self._plan is not None and not self._plan.based_on_cache_fresh:
            dias = int(edad.total_seconds() // 86400) if edad else 0
            aviso_caducada = (
                f"[b yellow]⚠ CACHÉ CADUCADA ({dias} d): los tipos/roles/"
                "archivados pueden estar desactualizados — refresca (r) antes "
                "de confirmar.[/]\n"
            )
        self.query_one("#plan-contadores", Static).update(
            f"[b]{len(self._plan.peers)} peers · {self._plan.destructive_count()} "
            f"operaciones destructivas[/]\n{conteo or '—'}\n"
            f"{aviso_caducada}"
            f"DRY-RUN: nada se ejecuta. Expira en {config.plan.ttl_min} min."
        )
        excluidos = "\n".join(f"— {pid}: {razon}" for pid, razon in self._plan.excluded)
        self.query_one("#plan-excluidos", Static).update(
            f"[b]Excluidos ({len(self._plan.excluded)}):[/] 🔒\n{excluidos or '—'}"
        )

    def action_volver(self) -> None:
        self.sentry_app.pop_screen()

    def action_confirmar(self) -> None:
        if not self._plan or not self._plan.peers:
            self.sentry_app.notify("el plan está vacío", severity="warning")
            return
        # doble-click no apila dos confirmaciones (3ª auditoría, P3)
        if isinstance(self.app.screen, ConfirmScreen):
            return
        self.sentry_app.push_screen(ConfirmScreen(self._plan))

    def on_checkbox_changed(self, _event: Checkbox.Changed) -> None:
        self._regenerar()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirmar":
            self.action_confirmar()
