"""Cliente Telethon con higiene de sesión y login interactivo 2FA.

Reglas de seguridad que este módulo hace cumplir:

- La sesión SQLite SIEMPRE vive en una ruta explícita (XDG por defecto),
  nunca en el CWD, en un directorio 0700 creado ANTES de que Telethon escriba.
- Tras cada conexión se fuerza 0600 en el archivo de sesión y su journal
  (Telethon los crea con 0644 según umask).
- StringSession solo mediante la variable de entorno
  TG_SENTRY_STRING_SESSION, nunca en config TOML ni logs; el valor se
  registra como secreto para su redacción.
- Teléfono, api_hash, códigos de login y contraseña 2FA se registran como
  secretos ANTES de cualquier operación que pueda loguearlos.

El login interactivo usa prompts bloqueantes de consola: pensado para el CLI
headless. La TUI (M4) tendrá su propia pantalla de auth sobre las mismas
primitivas.
"""

from __future__ import annotations

import contextlib
import getpass
import os
from collections.abc import Callable
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import tg_sentry.paths as paths
from tg_sentry import errors as tg_errors
from tg_sentry import redact

STRING_SESSION_ENV = "TG_SENTRY_STRING_SESSION"

PromptFn = Callable[[str], str]


class CredentialsError(Exception):
    """Credenciales ausentes o placeholder, con mensaje accionable."""


class PasswordRequired(Exception):
    """La cuenta tiene 2FA: el llamador debe pedir contraseña y reintentar."""


class PasswordInvalid(Exception):
    """Contraseña 2FA incorrecta: se puede reintentar."""


class SesionNoAutorizada(Exception):
    """La sesión no está autorizada: el camino es la pantalla login."""


def _validate_credentials(
    api_id: int, api_hash: str, phone: str, *, check_phone: bool = True
) -> None:
    if api_id <= 0 or not api_hash:
        msg = (
            "api_id/api_hash faltan o tienen valor placeholder. Obténlos A MANO "
            "en https://my.telegram.org (API development tools) y rellénalos en "
            "~/.config/tg-sentry/config.toml. Se validan intentando conectar; "
            "tg-sentry no scrapea ese sitio."
        )
        raise CredentialsError(msg)
    if check_phone and not phone:
        msg = (
            "Falta el teléfono (con prefijo de país) en [telegram] del config.toml; "
            "el CLI `tg-sentry login` lo pregunta interactivamente si falta."
        )
        raise CredentialsError(msg)


async def start_code_login(
    api_id: int, api_hash: str, phone: str, session: str | Path | None = None
) -> tuple[TelegramClient, str | None]:
    """Conecta (crea sesión si no existe) y envía el código de login.

    Devuelve (client, phone_code_hash). Si la sesión YA está autorizada,
    devuelve (client, None) sin enviar nada.
    """
    _validate_credentials(api_id, api_hash, phone)
    redact.register_secret(api_hash)
    redact.register_secret(phone)
    client = build_client(api_id, api_hash, session)
    await client.connect()
    harden_session_perms(session if not os.environ.get(STRING_SESSION_ENV) else None)
    if await client.is_user_authorized():
        return client, None
    try:
        sent = await client.send_code_request(phone)
    except BaseException:
        # sin fuga de cliente conectado: si pedir el código falla (FloodWait,
        # api_id inválido, red), el cliente se desconecta ANTES de propagar —
        # un reintento creaba un SEGUNDO cliente sobre el mismo .session
        # (SQLite) con riesgo de corruptela — auditoría 2026-09.
        with contextlib.suppress(Exception):
            await client.disconnect()
        raise
    return client, sent.phone_code_hash


async def finish_code_login(
    client: TelegramClient,
    phone: str,
    code: str,
    phone_code_hash: str | None,
    password: str | None = None,
    session: str | Path | None = None,
) -> None:
    """Completa el login con el código (y contraseña 2FA si aplica).

    Raises:
        PasswordRequired: la cuenta tiene 2FA y no se pasó contraseña;
            el llamador la pide y reintenta CON ella.
        PasswordInvalid: la contraseña 2FA es incorrecta (reintentable).
        CredentialsError: código inválido o expirado (hay que re-pedirlo).
    """
    redact.register_secret(code)
    if password:
        redact.register_secret(password)
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if password is None:
            raise PasswordRequired from None
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise PasswordInvalid from exc
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        msg = (
            "código inválido o expirado: reinicia el login para que Telegram "
            "te envíe uno nuevo"
        )
        raise CredentialsError(msg) from exc
    harden_session_perms(session if not os.environ.get(STRING_SESSION_ENV) else None)


def session_target(session: str | Path | None = None) -> Path:
    """Ruta de sesión efectiva: la dada, o la XDG por defecto."""
    return Path(session).expanduser() if session else paths.session_path()


def _resolve_session(session: str | Path | None = None) -> StringSession | str:
    """StringSession del env si existe; si no, ruta explícita con directorio 0700."""
    env_value = os.environ.get(STRING_SESSION_ENV, "")
    if env_value:
        redact.register_secret(env_value)
        return StringSession(env_value)
    target = session_target(session)
    paths.ensure_private_dir(target.parent)
    return str(target)


def build_client(api_id: int, api_hash: str, session: str | Path | None = None) -> TelegramClient:
    """Construye el TelegramClient sin conectar; registro de secretos incluido.

    flood_sleep_threshold=0: Telethon NO absorbe FloodWaits pequeños durmiendo
    en silencio dentro de __call__ — TODOS cruzan a execute_step, que los
    traduce y el motor los respeta y cuenta para el detector de patrón de
    baneo (auditoría 2026-09: con el default de 60s el monitor era ciego).
    """
    redact.register_secret(api_hash)
    return TelegramClient(
        _resolve_session(session), api_id, api_hash, flood_sleep_threshold=0
    )


def harden_session_perms(session: str | Path | None = None) -> None:
    """Fuerza 0600 en la sesión SQLite y su journal si existen."""
    target = session_target(session)
    for candidate in (target, target.with_name(target.name + "-journal")):
        if candidate.exists():
            candidate.chmod(0o600)


async def connect_cliente_autorizado(
    api_id: int, api_hash: str, session: str | Path | None = None
) -> TelegramClient:
    """Conecta y devuelve el cliente SOLO si la sesión ya está autorizada.

    Para la TUI: jamás interactúa ni envía código — si la sesión no está
    autorizada lanza SesionNoAutorizada (el camino es la pantalla login;
    un prompt bloqueante dentro de la TUI congela la UI y el loop de red,
    hallazgo auditoría 2026-09).
    """
    _validate_credentials(api_id, api_hash, phone="", check_phone=False)
    redact.register_secret(api_hash)
    client = build_client(api_id, api_hash, session)
    await client.connect()
    harden_session_perms(session if not os.environ.get(STRING_SESSION_ENV) else None)
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SesionNoAutorizada(
            "la sesión no está autorizada: reinicia tg-sentry y completa el login"
        )
    return client


async def connect_and_login(
    api_id: int,
    api_hash: str,
    phone: str,
    session: str | Path | None = None,
    *,
    prompt: PromptFn = input,
    password_prompt: PromptFn | None = None,
) -> TelegramClient:
    """Conecta y garantiza sesión autorizada; login interactivo si hace falta.

    Versión CLI (prompts bloqueantes). La TUI usa start_code_login/
    finish_code_login por etapas.

    Raises:
        CredentialsError: api_id/api_hash o teléfono ausentes/placeholder.
    """
    try:
        client, phone_code_hash = await start_code_login(api_id, api_hash, phone, session)
    except FloodWaitError as exc:
        # Telegram pide espera al enviar el código: taxonomía con segundos
        # (3ª auditoría: salía crudo y el CLI lo mostraba como traceback;
        # start_code_login ya desconectó el cliente antes de propagar)
        raise tg_errors.FloodWait(max(1, int(exc.seconds))) from exc
    if phone_code_hash is None:
        return client  # ya autorizado
    try:
        code = prompt("Código de Telegram: ")
        try:
            await finish_code_login(client, phone, code, phone_code_hash, session=session)
        except PasswordRequired:
            ask_pw = password_prompt or (lambda message: getpass.getpass(message))
            password = ask_pw("Contraseña 2FA: ")
            for intento in range(3):
                try:
                    await finish_code_login(
                        client,
                        phone,
                        code,
                        phone_code_hash,
                        password=password,
                        session=session,
                    )
                    break
                except PasswordInvalid as exc:
                    # el reintento es SIEMPRE posible: ask_pw tiene fallback
                    # getpass (3ª auditoría: `or password_prompt is None`
                    # mataba el bucle en CLI — una contraseña incorrecta era
                    # error sin segundas oportunidades)
                    if intento == 2:
                        msg = "contraseña 2FA incorrecta"
                        raise CredentialsError(msg) from exc
                    password = ask_pw("Contraseña 2FA incorrecta. Reintenta: ")
        return client
    except FloodWaitError as exc:
        # Telegram pide espera al enviar código: el CLI la traduce a mensaje
        # accionable (3ª auditoría: salía traceback crudo)
        raise tg_errors.FloodWait(max(1, int(exc.seconds))) from exc
    except BaseException:
        # Ctrl-C en el prompt, código inválido o 2FA agotado: el cliente
        # quedaba CONECTADO y huérfano agarrando el lock SQLite de la sesión
        # — el reintento del usuario creaba un 2º cliente sobre el mismo
        # .session ("database is locked") (2ª auditoría)
        with contextlib.suppress(Exception):
            await client.disconnect()
        raise


def segundos_flood(exc: BaseException) -> int | None:
    """Segundos de espera si `exc` es un FloodWait (Telethon o taxonomía).

    Para rutas de LECTURA (p. ej. iter_dialogs en dialogs --refresh) donde
    traducir dentro de inventory rompería el capado (inventory no importa
    Telethon): el llamador CLI consulta esto para dar mensaje accionable.
    """
    value = getattr(exc, "seconds", None)
    return value if isinstance(value, int) and value >= 0 else None
