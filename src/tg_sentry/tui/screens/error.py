"""Pantalla de error fatal de arranque (p. ej. config corrupta).

Antes un ConfigError en la TUI reventaba con la pantalla de crash de
Textual (traceback); ahora se muestra el mensaje accionable y se sale
limpio con 'q' (auditoría 2026-09).
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Static

from tg_sentry.tui.base import BaseScreen


class ErrorScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("q", "app.quit", "salir"),
        ("escape", "app.quit", "salir"),
    ]

    def __init__(self, mensaje: str) -> None:
        super().__init__()
        self._mensaje = mensaje

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="login-caja"):
            yield Static("[b red]No se puede arrancar tg-sentry[/]")
            yield Static(self._mensaje)
        yield Footer()
