"""Modelos de dominio de tg-sentry (pydantic, serializables a caché/export)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PeerKind(StrEnum):
    """Tipo de peer. SECRET: los chats secretos NO aparecen en el inventario
    (Telethon no los itera) y safety los excluye de todos modos — cinturón
    extra para planes manipulados, no una protección activa del pipeline."""

    BOT = "bot"
    USER = "user"
    GROUP = "group"
    CHANNEL = "channel"
    SECRET = "secret"
    UNKNOWN = "unknown"


class PeerRole(StrEnum):
    """Rol propio dentro del peer. None = aún no consultado (spike M1)."""

    CREATOR = "creator"
    ADMIN = "admin"
    MEMBER = "member"
    SUBSCRIBER = "subscriber"


class DialogInfo(BaseModel):
    """Vista serializable de un diálogo: lo único que vive en caché/export."""

    model_config = ConfigDict(extra="forbid")

    id: int
    kind: PeerKind
    name: str = ""
    username: str | None = None
    archived: bool = False
    pinned: bool = False
    muted: bool = False
    unread: int = 0
    last_message_at: datetime | None = None
    role: PeerRole | None = None

    @property
    def display(self) -> str:
        """Etiqueta humana: nombre, o username, o id."""
        return self.name or (f"@{self.username}" if self.username else str(self.id))
