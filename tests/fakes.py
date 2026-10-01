"""Fakes de objetos Telethon para tests. Cero red, cero cuenta real."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class FakeEntity:
    bot: bool = False
    username: str | None = None


@dataclass
class FakeNotifySettings:
    mute_until: datetime | None = None


@dataclass
class FakeInnerDialog:
    notify_settings: FakeNotifySettings = field(default_factory=FakeNotifySettings)


@dataclass
class FakeDialog:
    id: int
    name: str = ""
    entity: Any = None
    is_user: bool = False
    is_group: bool = False
    is_channel: bool = False
    archived: bool = False
    pinned: bool = False
    unread_count: int = 0
    date: datetime | None = None
    dialog: FakeInnerDialog = field(default_factory=FakeInnerDialog)


def sample_dialogs() -> list[FakeDialog]:
    """Set representativo: bot, usuario, grupo, canal, desconocido, silenciado."""
    old = datetime(2020, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 9, 19, tzinfo=UTC)
    return [
        FakeDialog(
            id=111, name="ejemplobot", entity=FakeEntity(bot=True, username="ejemplobot"),
            is_user=True, unread_count=0, date=old,
        ),
        FakeDialog(
            id=222, name="Usuario Ejemplo", entity=FakeEntity(username="usuario_ejemplo"),
            is_user=True, unread_count=2, date=recent,
        ),
        FakeDialog(
            id=-100333, name="Grupo Ejemplo", is_group=True, archived=True,
            unread_count=148675, date=recent,
        ),
        FakeDialog(
            id=-100444, name="Canal Alertas", is_channel=True,
            unread_count=25, date=old,
        ),
        FakeDialog(
            id=555, name="Desconocido",
        ),
        FakeDialog(
            id=-100666, name="Canal Ejemplo", is_channel=True,
            unread_count=51532, date=recent,
            dialog=FakeInnerDialog(FakeNotifySettings(mute_until=recent)),
        ),
    ]
