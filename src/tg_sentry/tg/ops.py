"""Operaciones primitivas contra Telegram.

INVARIANTE DE PROYECTO: las rarezas de la API de Telegram viven SOLO en este
módulo (y en tg/client.py). Ningún otro paquete toca objetos de Telethon.

Lectura: `to_dialog_info` mapea un Dialog al modelo.
Escritura: `execute_step` ejecuta un paso del plan con el cliente y traduce
las excepciones de Telethon a la taxonomía neutra de `tg_sentry.errors`
(semánticas a confirmar en el piloto — protocolo en documentación interna).

`clear_history` (vaciar historial SIN salir) no tiene primitiva segura
verificada en Telethon 1.x: queda UnsupportedStep hasta el piloto.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from telethon.errors import (
    AuthKeyDuplicatedError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodError,
    FrozenMethodInvalidError,
    InvalidBufferError,
    PeerIdInvalidError,
    RPCError,
    UnauthorizedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserNotParticipantError,
)
from telethon.tl.functions.contacts import BlockRequest, GetContactsRequest
from telethon.tl.types import InputPeerChannel

from tg_sentry.errors import (
    AdminRequired,
    FloodWait,
    PeerGone,
    RpcFailure,
    SessionInvalid,
    TransientNetwork,
    UnsupportedStep,
)
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import StepKind

# Timeout duro por paso: un RPC colgado (NAT drop sin RST) no puede bloquear
# el ejecutor para siempre (auditoría 2026-09). Generoso: cubre lotes de 100.
STEP_TIMEOUT_S = 120.0


def to_dialog_info(dialog: object) -> DialogInfo:
    """Mapea un telethon Dialog (o fake con los mismos atributos) a DialogInfo."""
    entity = getattr(dialog, "entity", None)

    if getattr(dialog, "is_user", False):
        kind = PeerKind.BOT if getattr(entity, "bot", False) else PeerKind.USER
    elif getattr(dialog, "is_group", False):
        kind = PeerKind.GROUP
    elif getattr(dialog, "is_channel", False):
        kind = PeerKind.CHANNEL
    else:
        kind = PeerKind.UNKNOWN

    inner = getattr(dialog, "dialog", None)
    notify_settings = getattr(inner, "notify_settings", None)
    # Simplificación M1: mute_until presente = silenciado (aunque haya expirado
    # hace poco); refinar en el spike si importa.
    muted = getattr(notify_settings, "mute_until", None) is not None

    last_message_at = getattr(dialog, "date", None)
    if last_message_at is not None and not isinstance(last_message_at, datetime):
        last_message_at = None

    return DialogInfo(
        id=int(getattr(dialog, "id", 0) or 0),
        kind=kind,
        name=str(getattr(dialog, "name", "") or ""),
        username=getattr(entity, "username", None),
        archived=bool(getattr(dialog, "archived", False)),
        pinned=bool(getattr(dialog, "pinned", False)),
        muted=muted,
        unread=int(getattr(dialog, "unread_count", 0) or 0),
        last_message_at=last_message_at,
        role=None,  # exige GetParticipant por peer: spike M1 + M2 safety
    )


async def execute_step(client: object, step: StepKind, dialog: DialogInfo) -> int | None:
    """Ejecuta un paso destructivo y traduce errores Telethon → taxonomía propia.

    Devuelve None normalmente; en pasos DELETE_OWN_* devuelve el número de
    mensajes borrados (el motor lo usa para métricas Y cooldown proporcional:
    los gateways de producción DEBEN propagarlo — auditoría 2026-09). Se
    resuelve el peer por id marcado (la sesión Telethon lo resuelve desde su
    caché); los errores de resolución se tratan como peer inaccesible.

    Todo paso corre bajo un timeout duro (STEP_TIMEOUT_S): un RPC colgado se
    traduce a TransientNetwork en vez de colgar la cola para siempre.
    """
    peer = dialog.id
    try:
        async with asyncio.timeout(STEP_TIMEOUT_S):
            return await _ejecutar(client, step, peer)
    except UnsupportedStep:
        raise  # nuestra propia excepción de contrato: sin traducir
    except FloodError as exc:
        # FloodError es la BASE: cubre FloodWaitError, FloodPremiumWaitError y
        # SlowModeWaitError (420); las dos últimas NO heredan de FloodWaitError
        # y antes caían a reintentos de 1-2s sin respetar la espera exigida.
        # Piso 1s: FloodWait(0) (capture vacío) con el motor era un bucle
        # caliente martilleando Telegram (2ª auditoría).
        # FrozenMethodInvalidError (cuenta congelada) es el único FloodError
        # SIN .seconds: es determinista y no se "espera" — mapea a
        # AdminRequired (fallo del ítem sin bucle) en vez de AttributeError.
        if isinstance(exc, FrozenMethodInvalidError):
            raise _traducida(
                AdminRequired("cuenta congelada/restringida: Telegram rechaza la operación"),
                exc,
            ) from exc
        raise _traducida(FloodWait(max(1, int(exc.seconds))), exc) from exc
    except UserNotParticipantError as exc:
        raise _traducida(PeerGone("not_participant"), exc) from exc
    except ChannelPrivateError as exc:
        raise _traducida(PeerGone("channel_private"), exc) from exc
    except PeerIdInvalidError as exc:
        raise _traducida(PeerGone("peer_invalid"), exc) from exc
    except (UserDeactivatedError, UserDeactivatedBanError) as exc:
        raise _traducida(PeerGone("user_deactivated"), exc) from exc
    except ChatAdminRequiredError as exc:
        raise _traducida(AdminRequired(str(exc)), exc) from exc
    except AuthKeyDuplicatedError as exc:
        # auth key duplicada: TERMINAL por diseño (2ª auditoría: caía a
        # RpcFailure y quemaba 5 reintentos por ítem con la sesión rota)
        raise _traducida(SessionInvalid(str(exc)), exc) from exc
    except UnauthorizedError as exc:
        # sesión revocada / auth key inválida: determinista y terminal;
        # reintentar solo quema el presupuesto (auditoría 2026-09)
        raise _traducida(SessionInvalid(str(exc)), exc) from exc
    except InvalidBufferError as exc:
        # buffer corrupto del servidor NO hereda de RPCError/OSError: salía
        # cruda y el motor la trataba como bug sin reintento (2ª auditoría).
        # Con code 404 el sender la trata como auth key muerta → terminal.
        if getattr(exc, "code", None) == 404:
            raise _traducida(SessionInvalid(str(exc)), exc) from exc
        raise _traducida(TransientNetwork(f"{type(exc).__name__}: {exc}"), exc) from exc
    except RPCError as exc:
        # RPC genérico (no mapeado arriba): reintentable, luego fallo del ítem
        raise _traducida(RpcFailure(f"{type(exc).__name__}: {exc}"), exc) from exc
    except ValueError as exc:
        # resolución de entidad fallida (peer fuera de la caché de sesión):
        # el docstring lo promete y antes se colaba crudo (auditoría 2026-09).
        # Un ValueError que NO es de resolución era tragado como peer inválido
        # (bug invisible → skipped_unreachable); ahora es RpcFailure del ítem
        # con reintentos y el mensaje visible (2ª auditoría).
        if "input entity" in str(exc) or "Could not find" in str(exc):
            raise _traducida(PeerGone("peer_invalid"), exc) from exc
        raise _traducida(RpcFailure(f"ValueError: {exc}"), exc) from exc
    except TimeoutError as exc:
        # nuestro asyncio.timeout: red que no responde → transitorio reintentable
        raise _traducida(
            TransientNetwork(f"timeout de paso ({int(STEP_TIMEOUT_S)}s)"), exc
        ) from exc
    except ConnectionError as exc:
        raise _traducida(TransientNetwork(str(exc)), exc) from exc
    except OSError as exc:  # TimeoutError/ConnectionError ya cubiertos arriba
        raise _traducida(TransientNetwork(str(exc)), exc) from exc
    except Exception as exc:
        # Red de seguridad (2ª auditoría): una excepción de Telethon sin
        # mapear (TypeNotFoundError, BadMessageError, SecurityError, …)
        # NUNCA sale cruda — antes reventaba el motor como "error inesperado"
        # sin reintentos. RpcFailure: 5 reintentos con backoff y, si sigue,
        # fallo del ÍTEM con la razón visible (el lote continúa, como manda
        # el plan); TransientNetwork además abortaría la cola entera.
        raise _traducida(RpcFailure(f"{type(exc).__name__}: {exc}"), exc) from exc


def _traducida(nuestra: Exception, original: BaseException) -> Exception:
    """Copia los adjuntos dinámicos de la excepción ORIGINAL (p. ej. el
    conteo `tg_sentry_borrados_parciales` de delete_own_messages) a la
    traducida. Sin esto el conteo moría en la traducción y el cooldown
    proporcional era código muerto en producción (2ª auditoría, P1)."""
    parcial = getattr(original, "tg_sentry_borrados_parciales", None)
    if isinstance(parcial, int):
        nuestra.tg_sentry_borrados_parciales = parcial  # type: ignore[attr-defined]
    return nuestra


async def _ejecutar(client: object, step: StepKind, peer: int) -> int | None:
    """Cuerpo del paso (sin traducción de errores; execute_step la envuelve)."""
    if step is StepKind.DELETE_CHAT:
        await client.delete_dialog(peer)  # type: ignore[attr-defined]
    elif step is StepKind.DELETE_CHAT_REVOKE:
        await client.delete_dialog(peer, revoke=True)  # type: ignore[attr-defined]
    elif step is StepKind.LEAVE:
        await client.delete_dialog(peer)  # type: ignore[attr-defined]
    elif step is StepKind.BLOCK:
        # Telethon NO tiene client.block(): API cruda (hallazgo del piloto)
        await client(BlockRequest(id=peer))  # type: ignore[operator]
    elif step is StepKind.DELETE_OWN_FOR_ALL:
        return await delete_own_messages(client, peer, for_all=True)
    elif step is StepKind.DELETE_OWN_FOR_ME:
        return await delete_own_messages(client, peer, for_all=False)
    elif step is StepKind.CLEAR_HISTORY:
        msg = "clear_history sin primitiva segura verificada (pendiente de piloto)"
        raise UnsupportedStep(msg)
    else:  # pragma: no cover — StepKind cerrado
        msg = f"paso no soportado: {step}"
        raise UnsupportedStep(msg)
    return None


async def delete_own_messages(
    client: object, peer: int, *, for_all: bool, max_messages: int = 100
) -> int:
    """Borra los MENSAJES PROPIOS del peer, en lotes de 100, del más viejo al más nuevo.

    Semántica 48h (dos OPERACIONES distintas por diseño del proyecto):
    - for_all=True  → revoke=True: borra para todos DENTRO de 48h; fuera de
      48h el SERVIDOR degrada automáticamente a solo-para-ti (privados y
      grupos). Jamás es error: el degradado cuenta como solo-para-ti.
    - for_all=False → revoke=False: SOLO para ti, siempre, sin límite de edad
      — PERO solo en chats privados, bots y grupos BÁSICOS. En canales y
      supergrupos la API borra SIEMPRE para todos (delete_messages enruta a
      channels.DeleteMessagesRequest, que no admite revoke): con for_all=False
      el paso se RECHAZA con UnsupportedStep antes de tocar nada (jamás se
      ejecuta un borrado "solo para ti" que el servidor haría para todos).

    Devuelve el total de mensajes borrados (pts_count REAL del servidor;
    auditoría 2026-09). El tope max_messages acota cada ejecución (lotes de
    100 = 1 request cada uno). Si un lote falla a mitad, el conteo parcial
    viaja adjunto a la excepción (`tg_sentry_borrados_parciales`) para que
    el motor aplique cooldown aunque el paso acabe en failed.
    """
    if not for_all:
        entidad = await client.get_input_entity(peer)  # type: ignore[attr-defined]
        if isinstance(entidad, InputPeerChannel):
            msg = (
                "delete_own 'solo para ti' no es posible en canales/supergrupos: "
                "la API borra siempre para todos. Usa delete_own 'all' o leave=False."
            )
            raise UnsupportedStep(msg)
    # reverse=True: la API itera de MÁS NUEVO a más viejo por defecto; el
    # contrato del paso es lo más viejo primero (auditoría 2026-09, P2).
    ids: list[int] = []
    async for message in client.iter_messages(  # type: ignore[attr-defined]
        peer, from_user="me", limit=max_messages, reverse=True
    ):
        ids.append(message.id)
    borrados = 0
    try:
        for inicio in range(0, len(ids), 100):
            lote = ids[inicio : inicio + 100]
            resultado = await client.delete_messages(  # type: ignore[attr-defined]
                peer, lote, revoke=for_all
            )
            conteo = sum(
                int(getattr(r, "pts_count", 0) or 0) for r in (resultado or [])
            )
            borrados += conteo if conteo > 0 else len(lote)
    except BaseException as exc:
        # solo ADJUNTA el conteo parcial y re-lanza: nada se traga. El
        # atributo dinámico es deliberado: la excepción es de Telethon.
        exc.tg_sentry_borrados_parciales = borrados  # type: ignore[attr-defined]
        raise
    return borrados


async def contact_user_ids(client: object) -> set[int]:
    """Ids de los contactos de la cuenta (lectura, una llamada GetContacts).

    safety los usa como protección DURA de chats privados: si esta llamada
    falla, el error sube (el refresh aborta) — un fallo de contactos jamás
    degrada la protección en silencio.
    """
    result = await client(GetContactsRequest(hash=""))  # type: ignore[operator]
    return {user.id for user in getattr(result, "users", []) or []}


class TeleOpsGateway:
    """Adaptador cliente→motor: el ejecutor solo ve esta interfaz."""

    def __init__(self, client: object) -> None:
        self._client = client

    async def run_step(self, step: StepKind, dialog: DialogInfo) -> int | None:
        # PROPAGA el conteo de borrados: sin él el cooldown proporcional
        # (M6.2) era código muerto en producción — auditoría 2026-09, P2.
        return await execute_step(self._client, step, dialog)
