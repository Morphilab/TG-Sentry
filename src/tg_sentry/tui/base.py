"""Base de pantallas: app tipada como SentryApp.

Textual declara `Screen.app` como `App[Any]`; con esta propiedad las
pantallas acceden a `self.sentry_app.config` con tipos reales.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.screen import Screen

if TYPE_CHECKING:
    from tg_sentry.tui.app import SentryApp


class BaseScreen(Screen[None]):
    @property
    def sentry_app(self) -> SentryApp:
        return cast("SentryApp", self.app)
