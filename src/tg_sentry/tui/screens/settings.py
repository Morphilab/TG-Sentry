"""Ajustes desde la TUI: whitelist y protecciones (persistidas), límites vista.

Los límites NO se editan aquí por diseño: solo se muestran (configurarlos a
la baja exige editar el TOML; subirlos además --ack-over-limit en CLI).
whitelist y protecciones sí se editan y se guardan con comentarios preservados.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static

from tg_sentry.config import save_config
from tg_sentry.limits import (
    DEFAULT_BLOCK_PER_HOUR,
    DEFAULT_LEAVE_GROUPS_PER_HOUR,
    MAX_DESTRUCTIVE_PER_DAY,
    MAX_DESTRUCTIVE_PER_HOUR,
)
from tg_sentry.tui.base import BaseScreen


class SettingsScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        # honesto: Escape NO guarda (antes la etiqueta mentía — auditoría 2026-09)
        ("escape", "volver", "volver (DESCARTA cambios)"),
    ]

    def compose(self) -> ComposeResult:
        config = self.sentry_app.config
        safety = config.safety
        limits = config.limits
        yield Header(show_clock=False)
        with Vertical(id="settings-caja"):
            yield Label("[b]Protecciones[/]")
            yield Checkbox(
                "Protección dura: NO tocar chats privados de contactos",
                safety.protect_contacts,
                id="s-contacts",
            )
            yield Input(
                placeholder="días de actividad reciente protegida (privados)",
                value=str(safety.recent_private_days),
                id="s-recent",
                restrict=r"[0-9]*",
            )
            yield Input(
                placeholder="whitelist separada por comas (ids o @usernames)",
                value=", ".join(safety.whitelist),
                id="s-whitelist",
            )
            yield Label("[b]Límites (solo lectura aquí — edítalos en el TOML)[/]")
            yield Static(
                f"ritmo: {limits.writes_per_min} ops/min (default 4, subir exige "
                f"--ack-over-limit)\n"
                f"salir de grupos: {limits.leave_groups_per_hour}/h (default "
                f"{DEFAULT_LEAVE_GROUPS_PER_HOUR})\n"
                f"bloquear: {limits.block_per_hour}/h (default {DEFAULT_BLOCK_PER_HOUR})\n"
                f"[b]topes duros de sesión: {MAX_DESTRUCTIVE_PER_HOUR}/h · "
                f"{MAX_DESTRUCTIVE_PER_DAY}/día (NO configurables)[/]",
                id="s-limits",
            )
            with Horizontal(id="settings-botones"):
                yield Button("Guardar y volver", id="btn-guardar", variant="primary")
            yield Static("", id="s-estado")
        yield Footer()

    def _guardar(self) -> bool:
        """Guarda la config; False si falló (el fallo NUNCA tumba la TUI:
        ValueError del número, OSError del save_config — 3ª auditoría)."""
        try:
            config = self.sentry_app.config
            recientes = self.query_one("#s-recent", Input).value.strip()
            whitelist = [
                token.strip()
                for token in self.query_one("#s-whitelist", Input).value.split(",")
                if token.strip()
            ]
            # la whitelist se VALIDA aquí: una entrada malformada ("--77")
            # guardada rompería el dashboard/plan al recargar (4ª auditoría)
            from tg_sentry.safety import parse_whitelist

            parse_whitelist(whitelist)
            config.safety.protect_contacts = self.query_one("#s-contacts", Checkbox).value
            # campo vacío = conserva el valor actual (no lo resetea a 30 en silencio)
            config.safety.recent_private_days = (
                int(recientes) if recientes else config.safety.recent_private_days
            )
            config.safety.whitelist = whitelist
            save_config(config)
        except (ValueError, OSError) as exc:
            self.query_one("#s-estado", Static).update(
                f"[red]✗ no guardado: {type(exc).__name__}: {exc}[/]"
            )
            return False
        self.query_one("#s-estado", Static).update("[green]✓ guardado[/]")
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-guardar" and self._guardar():
            self.action_volver()

    def action_volver(self) -> None:
        # pop (no switch): se abre con push desde el dashboard; conserva la
        # selección viva del dashboard (3ª auditoría)
        self.sentry_app.pop_screen()
