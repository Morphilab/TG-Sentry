"""Tests de higiene de sesión (sin conectar, sin red)."""

from __future__ import annotations

import stat
from pathlib import Path

from tg_sentry.tg.client import session_target


def test_session_target_por_defecto_es_xdg(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert session_target() == tmp_path / "tg-sentry" / "tg-sentry.session"


def test_session_target_con_ruta_explicita(tmp_path: Path) -> None:
    assert session_target(tmp_path / "otra.session") == tmp_path / "otra.session"


def test_harden_session_perms_fuerza_0600_en_sesion_y_journal(tmp_path: Path) -> None:
    from tg_sentry.tg.client import harden_session_perms

    session = tmp_path / "x.session"
    journal = tmp_path / "x.session-journal"
    for file in (session, journal):
        file.write_bytes(b"data")
        file.chmod(0o644)
    harden_session_perms(session)
    assert stat.S_IMODE(session.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


def test_harden_session_perms_sin_archivos_no_explota(tmp_path: Path) -> None:
    from tg_sentry.tg.client import harden_session_perms

    harden_session_perms(tmp_path / "inexistente.session")


def test_start_code_login_desconecta_si_falla_is_user_authorized(
    monkeypatch, tmp_path: Path
) -> None:
    """el patrón disconnect-antes-de-propagar solo cubría
    send_code_request — un fallo en is_user_authorized dejaba el cliente
    CONECTADO huérfano (en la TUI, dentro del runtime: set_client jamás lo
    veía, y el reintento creaba un SEGUNDO cliente sobre el mismo .session)."""
    import asyncio

    import pytest

    from tg_sentry.tg import client as mod

    class ClienteQueFalla:
        def __init__(self) -> None:
            self.conectado = False
            self.desconectado = False

        async def connect(self) -> None:
            self.conectado = True

        async def is_user_authorized(self) -> bool:
            raise ConnectionError("red caída tras conectar")

        async def disconnect(self) -> None:
            self.desconectado = True

    falso = ClienteQueFalla()
    monkeypatch.setattr(mod, "build_client", lambda *a, **k: falso)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(ConnectionError):
        asyncio.run(mod.start_code_login(1, "a" * 32, "+10000000000", None))
    assert falso.conectado
    assert falso.desconectado  # sin huérfano: se desconecta ANTES de propagar
