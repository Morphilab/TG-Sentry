"""Pantalla de login por etapas: teléfono → código → 2FA.

 Usa las primitivas start_code_login/finish_code_login (nunca prompts
bloqueantes). Solo aparece si no hay sesión autorizada.
"""

from __future__ import annotations

import contextlib
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Static

import tg_sentry.paths as paths
from tg_sentry.tg.client import PasswordRequired, start_code_login
from tg_sentry.tui.base import BaseScreen


class LoginScreen(BaseScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "app.quit", "salir")
    ]
    AUTO_FOCUS = "#telefono"

    def __init__(self) -> None:
        super().__init__()
        self._client: object | None = None
        self._phone_code_hash: str | None = None
        self._phone = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="login-caja") as caja:
            caja.border_title = "Login de Telegram"
            yield Label("No hay sesión autorizada. Ingresa tu teléfono con prefijo de país.")
            yield Input(placeholder="+34600000000", id="telefono", restrict=r"[+0-9]*")
            yield Input(placeholder="Código recibido en Telegram", id="codigo", restrict=r"[0-9]*")
            yield Input(
                placeholder="Contraseña 2FA (solo si tu cuenta la tiene)",
                id="password",
                password=True,
            )
            yield Button("Conectar y enviar código", variant="primary", id="conectar")
            yield Button("Completar login", variant="warning", id="entrar")
            yield Static("", id="estado")
        yield Footer()

    def _estado(self, texto: str, error: bool = False) -> None:
        self.query_one("#estado", Static).update(
            f"[b {'red' if error else 'green'}]{texto}[/]"
        )

    def _telefono(self) -> str:
        return self.query_one("#telefono", Input).value.strip()

    def _botones(self) -> list[Button]:
        return [self.query_one("#conectar", Button), self.query_one("#entrar", Button)]

    def _bloquear(self, bloqueados: bool) -> None:
        """Botones fuera de servicio mientras el worker corre (2ª auditoría:
        un segundo click cancelaba el worker a mitad de start_code_login y el
        cliente ya conectado quedaba HUÉRFANO agarrando el lock de la sesión)."""
        for boton in self._botones():
            boton.disabled = bloqueados

    async def _conectar(self) -> None:
        telefono = self._telefono()
        if not telefono:
            self._estado("Escribe tu teléfono primero (ej. +34600000000).", error=True)
            return
        self._bloquear(True)
        try:
            return await self._conectar_bloqueado()
        finally:
            self._bloquear(False)

    async def _conectar_bloqueado(self) -> None:
        telefono = self._telefono()
        try:
            telegram = self.sentry_app.config.telegram
        except Exception as exc:  # config corrupta: mensaje, no crash
            self._estado(f"Error de configuración: {exc}", error=True)
            return
        # desconecta un cliente previo de un intento anterior: dos clientes
        # sobre el MISMO .session SQLite = "database is locked" y riesgo de
        # sesión corrupta (auditoría 2026-09)
        if self._client is not None:
            previo, self._client = self._client, None
            self._phone_code_hash = None
            with contextlib.suppress(Exception):
                await self.sentry_app.telethon_call(previo.disconnect())  # type: ignore[attr-defined]
        try:
            client, code_hash = await self.sentry_app.telethon_call(
                start_code_login(
                    telegram.api_id, telegram.api_hash, telefono, telegram.session or None
                )
            )
        except Exception as exc:  # credenciales o red: mensaje accionable
            self._estado(f"Error: {exc}", error=True)
            return
        self._client = client
        # registrar YA en la app (no solo al finalizar): si el usuario sale con
        # el login a mitad (escape = quit), on_exit_app desconecta este cliente
        # en vez de dejarlo fugado hasta morir el proceso (3ª auditoría)
        self.sentry_app.set_client(client)
        self._phone = telefono
        if code_hash is None:
            self._finalizar()
            return
        self._phone_code_hash = code_hash
        self._estado("Código enviado. Revisa Telegram y pulsa 'Completar login'.")

    async def _entrar(self) -> None:
        if self._client is None:
            self._estado("Conecta primero (botón 'Conectar y enviar código').", error=True)
            return
        codigo = self.query_one("#codigo", Input).value.strip()
        if not codigo:
            self._estado("Escribe el código recibido.", error=True)
            return
        self._bloquear(True)
        try:
            await self._entrar_bloqueado()
        finally:
            self._bloquear(False)

    async def _entrar_bloqueado(self) -> None:
        codigo = self.query_one("#codigo", Input).value.strip()
        from tg_sentry.tg.client import finish_code_login

        password = self.query_one("#password", Input).value or None
        try:
            await self.sentry_app.telethon_call(
                finish_code_login(
                    self._client,
                    self._phone,
                    codigo,
                    self._phone_code_hash,
                    password=password,
                )
            )
        except PasswordRequired:
            self._estado("Tu cuenta tiene 2FA: escribe la contraseña y reintenta.", error=True)
            return
        except Exception as exc:
            self._estado(f"Error: {exc}", error=True)
            return
        self._finalizar()

    def _finalizar(self) -> None:
        if self._client is not None:
            self.sentry_app.set_client(self._client)
        sesion = paths.session_path()
        modo = oct(paths.file_mode(sesion) or 0)
        self._estado(f"✓ Sesión autorizada ({sesion}, permisos {modo}).")
        self.sentry_app.notify("Sesión autorizada")
        self.sentry_app.after_login()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # funciones, no coroutines invocadas: un worker descartado antes de
        # arrancar no deja una coroutine sin await (4ª auditoría)
        if event.button.id == "conectar":
            self.run_worker(self._conectar, exclusive=True)  # type: ignore[arg-type]
        elif event.button.id == "entrar":
            self.run_worker(self._entrar, exclusive=True)  # type: ignore[arg-type]
