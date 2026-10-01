"""Tests del CLI login: prompt de teléfono cuando falta en config. Sin red."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tg_sentry.__main__ import EXIT_CONFIG, _cmd_login
from tg_sentry.config import EXAMPLE_TOML, Config, load_config, save_config
from tg_sentry.tg.client import CredentialsError, connect_and_login


def test_credentials_error_sin_telefono_menciona_el_prompt() -> None:
    with pytest.raises(CredentialsError, match="lo pregunta interactivamente"):
        asyncio.run(connect_and_login(12345, "a" * 32, ""))


def test_config_sin_telefono_se_completa_y_guarda(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(EXAMPLE_TOML, encoding="utf-8")
    config = load_config(file)
    assert config.telegram.phone == ""

    config.telegram.phone = "+34600000000"
    save_config(config, file)
    assert load_config(file).telegram.phone == "+34600000000"


def test_cmd_login_prompt_vacio_devuelve_error_de_config(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda *_args: "")
    config = Config()
    config.telegram.api_id = 1
    config.telegram.api_hash = "a" * 32
    assert asyncio.run(_cmd_login(config)) == EXIT_CONFIG


# --- Regresiones auditoría 2026-09 (rama api: login) ------------------------


class _FakeCliente:
    """Sustituye a TelegramClient en build_client (monkeypatch). Sin red."""

    def __init__(self) -> None:
        self.conectado = False
        self.desconectado = False
        self.codigos_enviados = 0
        self.fallar_codigo = False
        self.contrasenas: list[str | None] = []
        self.fallar_password_n_veces = 0

    async def connect(self) -> bool:
        self.conectado = True
        return True

    async def is_user_authorized(self) -> bool:
        return False

    async def disconnect(self) -> None:
        self.desconectado = True

    async def send_code_request(self, phone: str):  # type: ignore[no-untyped-def]
        if self.fallar_codigo:
            import telethon.errors as te

            raise te.FloodWaitError(request=None, capture=99)
        self.codigos_enviados += 1

        class _Sent:
            phone_code_hash = "hash-1"

        return _Sent()

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):  # type: ignore[no-untyped-def]
        import telethon.errors as te

        if password is None:
            raise te.SessionPasswordNeededError(request=None)
        self.contrasenas.append(password)
        if self.fallar_password_n_veces > 0:
            self.fallar_password_n_veces -= 1
            raise te.PasswordHashInvalidError(request=None)


def test_start_code_login_desconecta_si_envio_de_codigo_falla(monkeypatch) -> None:
    """Sin fuga de cliente: si send_code_request explota, el cliente se
    desconecta ANTES de propagar (2 clientes sobre el mismo .session = riesgo
    de corruptela en el reintento — auditoría 2026-09)."""
    import telethon.errors as te

    from tg_sentry.tg import client as cliente

    fake = _FakeCliente()
    fake.fallar_codigo = True
    monkeypatch.setattr(cliente, "build_client", lambda *_a, **_k: fake)

    async def run() -> None:
        try:
            await cliente.start_code_login(1, "a" * 32, "+34600000000")
            raise AssertionError("debía propagar el FloodWait")
        except te.FloodWaitError:
            pass
        assert fake.desconectado is True

    asyncio.run(run())


def test_connect_and_login_reintenta_contraseña_2fa(monkeypatch) -> None:
    from tg_sentry.tg import client as cliente

    fake = _FakeCliente()
    fake.fallar_password_n_veces = 1  # primera mala, segunda buena
    monkeypatch.setattr(cliente, "build_client", lambda *_a, **_k: fake)
    codigos = iter(["11111"])
    contrasenas = iter(["mala", "buena"])

    async def run() -> None:
        devuelto = await cliente.connect_and_login(
            1,
            "a" * 32,
            "+34600000000",
            prompt=lambda *_a: next(codigos),
            password_prompt=lambda *_a: next(contrasenas),
        )
        assert devuelto is fake
        assert fake.contrasenas == ["mala", "buena"]

    asyncio.run(run())


def test_connect_and_login_codigoinvalido_es_credentials_error(monkeypatch) -> None:
    import telethon.errors as te

    from tg_sentry.tg import client as cliente

    fake = _FakeCliente()

    async def sign_in_roto(**kwargs):  # type: ignore[no-untyped-def]
        raise te.PhoneCodeInvalidError(request=None)

    fake.sign_in = sign_in_roto  # type: ignore[method-assign]
    monkeypatch.setattr(cliente, "build_client", lambda *_a, **_k: fake)
    codigos = iter(["11111"])

    async def run() -> None:
        try:
            await cliente.connect_and_login(
                1, "a" * 32, "+34600000000", prompt=lambda *_a: next(codigos)
            )
            raise AssertionError("debía fallar con CredentialsError")
        except CredentialsError as exc:
            assert "código" in str(exc).lower()

    asyncio.run(run())


def test_connect_cliente_autorizado_niega_sin_sesion(monkeypatch, tmp_path: Path) -> None:
    from tg_sentry.tg import client as cliente

    fake = _FakeCliente()

    async def autorizado() -> bool:
        return False

    fake.is_user_authorized = autorizado  # type: ignore[method-assign]
    monkeypatch.setattr(cliente, "build_client", lambda *_a, **_k: fake)

    async def run() -> None:
        try:
            await cliente.connect_cliente_autorizado(1, "a" * 32, tmp_path / "s.session")
            raise AssertionError("debía negar la sesión")
        except cliente.SesionNoAutorizada:
            assert fake.desconectado is True  # no deja sockets abiertos

    asyncio.run(run())


def test_connect_and_login_reintenta_2fa_sin_prompt_inyectado(monkeypatch) -> None:
    """P2 3ª auditoría: el reintento de contraseña 2FA era CÓDIGO MUERTO en
    CLI (`or password_prompt is None`): una contraseña incorrecta era error
    sin segundas oportunidades. Con getpass (vía monkeypatch) reintenta."""
    import getpass

    from tg_sentry.tg import client as cliente

    fake = _FakeCliente()
    fake.fallar_password_n_veces = 1
    monkeypatch.setattr(cliente, "build_client", lambda *_a, **_k: fake)
    codigos = iter(["11111"])
    contrasenas = iter(["mala", "buena"])
    monkeypatch.setattr(
        getpass, "getpass", lambda *_a: next(contrasenas)
    )

    async def run() -> None:
        devuelto = await cliente.connect_and_login(
            1,
            "a" * 32,
            "+34600000000",
            prompt=lambda *_a: next(codigos),
        )
        assert devuelto is fake
        assert fake.contrasenas == ["mala", "buena"]

    asyncio.run(run())


def test_connect_and_login_traduce_floodwait_y_desconecta(monkeypatch) -> None:
    """P2 3ª auditoría: el FloodWait al enviar código salía CRUDO de
    connect_and_login y el CLI lo mostraba como traceback. Ahora sale como
    taxonomía FloodWait (con segundos) y el cliente queda desconectado."""

    from tg_sentry import errors as tg_errors
    from tg_sentry.tg import client as cliente

    fake = _FakeCliente()
    fake.fallar_codigo = True
    monkeypatch.setattr(cliente, "build_client", lambda *_a, **_k: fake)

    async def run() -> None:
        try:
            await cliente.connect_and_login(1, "a" * 32, "+34600000000")
            raise AssertionError("debía traducir el FloodWait")
        except tg_errors.FloodWait as exc:
            assert exc.seconds == 99
        assert fake.desconectado is True

    asyncio.run(run())


def test_segundos_flood_ayuda_duck_typing() -> None:
    from tg_sentry.errors import FloodWait as TgFloodWait
    from tg_sentry.tg.client import segundos_flood

    assert segundos_flood(TgFloodWait(42)) == 42

    class _TelethonFlood:
        seconds = 7

    assert segundos_flood(_TelethonFlood()) == 7
    assert segundos_flood(ValueError("no flood")) is None
