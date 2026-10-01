"""Tests del mapeo dialog→DialogInfo (tg/ops.py) con fakes."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.fakes import sample_dialogs
from tg_sentry.models import PeerKind
from tg_sentry.tg.ops import to_dialog_info


def test_mapeo_tipos_correcto() -> None:
    mapped = [to_dialog_info(d) for d in sample_dialogs()]
    kinds = [d.kind for d in mapped]
    assert kinds == [
        PeerKind.BOT,
        PeerKind.USER,
        PeerKind.GROUP,
        PeerKind.CHANNEL,
        PeerKind.UNKNOWN,
        PeerKind.CHANNEL,
    ]


def test_mapeo_campos_basicos() -> None:
    mapped = {d.id: d for d in map(to_dialog_info, sample_dialogs())}
    bot = mapped[111]
    assert bot.username == "ejemplobot"
    assert bot.last_message_at == datetime(2020, 1, 1, tzinfo=UTC)

    grupo = mapped[-100333]
    assert grupo.archived is True
    assert grupo.unread == 148675


def test_mapeo_muted_por_notify_settings() -> None:
    mapped = {d.id: d for d in map(to_dialog_info, sample_dialogs())}
    assert mapped[-100666].muted is True
    assert mapped[-100444].muted is False


def test_mapeo_role_none_hasta_spike() -> None:
    for dialog in map(to_dialog_info, sample_dialogs()):
        assert dialog.role is None


def test_mapeo_fecha_basura_se_descarta() -> None:
    from tests.fakes import FakeDialog

    roto = FakeDialog(id=9, name="x", is_user=True, entity=None, date="no-soy-fecha")
    assert to_dialog_info(roto).last_message_at is None
