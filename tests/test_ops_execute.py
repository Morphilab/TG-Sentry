"""Tests de las primitivas tg/ops.execute_step con cliente falso. Sin red."""

from __future__ import annotations

import asyncio

import pytest
from telethon.errors import (
    AuthKeyDuplicatedError,
    InvalidBufferError,
    RPCError,
)
from telethon.tl.types import InputPeerChat

from tg_sentry.errors import (
    AdminRequired,
    FloodWait,
    PeerGone,
    RpcFailure,
    SessionInvalid,
    TransientNetwork,
)
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import StepKind
from tg_sentry.tg.ops import execute_step


class FakeClient:
    """Registra llamadas; scriptable por tipo de error a levantar."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._raise = raise_exc

    async def delete_dialog(self, peer: int, revoke: bool = False) -> None:
        self.calls.append(("delete_dialog", peer))
        if self._raise:
            raise self._raise

    async def __call__(self, request: object) -> None:
        self.calls.append(("raw", type(request).__name__))
        if self._raise:
            raise self._raise


def _bot() -> DialogInfo:
    return DialogInfo(id=12345, kind=PeerKind.BOT, name="b", username="b_bot")


def test_block_usa_blockrequest_por_api_cruda() -> None:
    async def run() -> None:
        client = FakeClient()
        await execute_step(client, StepKind.BLOCK, _bot())
        # regresión del piloto: Telethon NO tiene client.block()
        assert client.calls == [("raw", "BlockRequest")]

    asyncio.run(run())


def test_delete_chat_y_leave_usan_delete_dialog() -> None:
    async def run() -> None:
        client = FakeClient()
        await execute_step(client, StepKind.DELETE_CHAT, _bot())
        await execute_step(client, StepKind.LEAVE, _bot())
        assert client.calls == [("delete_dialog", 12345), ("delete_dialog", 12345)]

    asyncio.run(run())


def test_delete_chat_revoke_pasa_revoke_true() -> None:
    async def run() -> None:
        seen: list[bool] = []

        class RevokeSpy(FakeClient):
            async def delete_dialog(self, peer: int, revoke: bool = False) -> None:
                seen.append(revoke)
                await super().delete_dialog(peer, revoke)

        client = RevokeSpy()
        await execute_step(client, StepKind.DELETE_CHAT_REVOKE, _bot())
        assert seen == [True]

    asyncio.run(run())


def test_rpcerror_generico_se_traduce_a_rpcfailure() -> None:
    async def run() -> None:
        client = FakeClient(raise_exc=RPCError(None, "BOOM", 500))
        try:
            await execute_step(client, StepKind.BLOCK, _bot())
            raise AssertionError("debía traducir a RpcFailure")
        except RpcFailure as exc:
            assert "RPCError" in str(exc)

    asyncio.run(run())


def test_connectionerror_se_traduce_a_transient() -> None:
    async def run() -> None:
        client = FakeClient(raise_exc=ConnectionError("reset"))
        try:
            await execute_step(client, StepKind.BLOCK, _bot())
            raise AssertionError("debía traducir a TransientNetwork")
        except TransientNetwork:
            pass

    asyncio.run(run())


def test_peer_invalid_y_floodwait_se_traducen() -> None:
    from telethon.errors import FloodWaitError, PeerIdInvalidError

    async def run() -> None:
        # FloodWaitError(request, capture); .seconds lo asigna Telethon al capturar
        flood = FloodWaitError(request=None, capture=7)
        client = FakeClient(raise_exc=flood)
        try:
            await execute_step(client, StepKind.BLOCK, _bot())
            raise AssertionError
        except FloodWait as translated:
            assert translated.seconds == 7

        client2 = FakeClient(raise_exc=PeerIdInvalidError(request=None))
        try:
            await execute_step(client2, StepKind.BLOCK, _bot())
            raise AssertionError
        except PeerGone as gone:
            assert gone.reason == "peer_invalid"

    asyncio.run(run())


# --- Regresiones de la auditoría 2026-09 (rama api/telethon) ----------------


def test_flood_error_base_cubre_premium_y_slowmode() -> None:
    """420 FLOOD_PREMIUM_WAIT y SLOWMODE_WAIT NO heredan de FloodWaitError:
    antes caían a RpcFailure (reintento de 1-2s sin respetar la espera)."""
    from telethon.errors import FloodPremiumWaitError, SlowModeWaitError

    for cls in (FloodPremiumWaitError, SlowModeWaitError):
        async def run(cls=cls) -> None:
            client = FakeClient(raise_exc=cls(request=None, capture=33))
            try:
                await execute_step(client, StepKind.BLOCK, _bot())
                raise AssertionError("debía traducir a FloodWait")
            except FloodWait as exc:
                assert exc.seconds == 33

        asyncio.run(run())


def test_valueerror_de_resolucion_es_peer_gone() -> None:
    """'Could not find the input entity' sale como ValueError crudo del
    cliente: era error inesperado en vez de skipped_unreachable."""
    async def run() -> None:
        client = FakeClient(raise_exc=ValueError("Could not find the input entity"))
        try:
            await execute_step(client, StepKind.DELETE_CHAT, _bot())
            raise AssertionError("debía traducir a PeerGone")
        except PeerGone as exc:
            assert exc.reason == "peer_invalid"

    asyncio.run(run())


def test_unauthorized_es_sesion_invalida_terminal() -> None:
    from telethon.errors import AuthKeyUnregisteredError, UnauthorizedError

    casos = [(UnauthorizedError, (None, "revocada")), (AuthKeyUnregisteredError, (None,))]
    for cls, args in casos:
        async def run(cls=cls, args=args) -> None:
            client = FakeClient(raise_exc=cls(*args))
            try:
                await execute_step(client, StepKind.BLOCK, _bot())
                raise AssertionError("debía traducir a SessionInvalid")
            except SessionInvalid:
                pass

        asyncio.run(run())


def test_timeout_de_paso_se_traduce_a_transient(monkeypatch) -> None:
    """Un RPC colgado no puede bloquear la cola para siempre."""
    import tg_sentry.tg.ops as ops

    monkeypatch.setattr(ops, "STEP_TIMEOUT_S", 0.05)

    class Colgado(FakeClient):
        async def delete_dialog(self, peer: int, revoke: bool = False) -> None:
            await asyncio.sleep(5)

    async def run() -> None:
        try:
            await execute_step(Colgado(), StepKind.DELETE_CHAT, _bot())
            raise AssertionError("debía saltar el timeout")
        except TransientNetwork as exc:
            assert "timeout" in str(exc)

    asyncio.run(run())


class _OwnClientFalla:
    """delete_own: primer lote OK (100 msgs), segundo lota revienta."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.lotes = 0

    async def get_input_entity(self, peer: int):  # type: ignore[no-untyped-def]
        return InputPeerChat(chat_id=abs(peer))

    def iter_messages(self, peer: int, *, from_user: str, limit: int, reverse: bool = False):  # type: ignore[no-untyped-def]
        # excede el limit del default (100) para forzar DOS lotes: la
        # excepción debe salir en el 2º con 100 borrados parciales
        async def _gen():
            for i in range(150):
                yield _MsgFake(1000 + i)

        return _gen()

    async def delete_messages(self, peer: int, ids: list[int], *, revoke: bool):  # type: ignore[no-untyped-def]
        self.lotes += 1
        if self.lotes >= 2:
            raise self._exc
        return [{"pts_count": len(ids)}] * len(ids)


class _MsgFake:
    def __init__(self, id: int) -> None:
        self.id = id


def test_authkey_duplicada_es_sesion_invalida_terminal() -> None:
    """2ª auditoría: caía a RpcFailure y quemaba 5 reintentos por ítem con
    la sesión rota; ahora es terminal."""
    async def run() -> None:
        client = FakeClient(AuthKeyDuplicatedError(request=None))
        with pytest.raises(SessionInvalid):
            await execute_step(client, StepKind.BLOCK, _bot())

    asyncio.run(run())


def test_invalid_buffer_error_se_traduce() -> None:
    """2ª auditoría: no hereda de RPCError/OSError — salía cruda y el motor
    la tomaba por bug sin reintentos."""
    async def run() -> None:
        client = FakeClient(InvalidBufferError(b"payload"))
        with pytest.raises(TransientNetwork):
            await execute_step(client, StepKind.BLOCK, _bot())

    asyncio.run(run())


def test_valueerror_ajeno_a_resolucion_es_rpcfailure() -> None:
    """2ª auditoría: un ValueError de bug se tragaba como peer inválido
    (skipped_unreachable, bug invisible); ahora es fallo de ítem visible."""
    async def run() -> None:
        client = FakeClient(ValueError("parse roto"))
        with pytest.raises(RpcFailure):
            await execute_step(client, StepKind.BLOCK, _bot())

    asyncio.run(run())


def test_borrados_parciales_sobrevive_la_traduccion() -> None:
    """2ª auditoría P1: el conteo parcial viaja adjunto a la excepción
    ORIGINAL de Telethon; sin copiarlo a la traducida, el cooldown
    proporcional era código muerto en producción."""
    async def run() -> None:
        exc = RPCError(None, "boom", 500)
        client = _OwnClientFalla(exc)
        with pytest.raises(RpcFailure) as info:
            await execute_step(client, StepKind.DELETE_OWN_FOR_ME, _bot())
        from tg_sentry.executor import ExecutionEngine

        # la traducida lleva el adjunto Y el motor lo encuentra vía __cause__
        parcial = getattr(info.value, "tg_sentry_borrados_parciales", None)
        assert parcial == 100
        assert ExecutionEngine._borrados_parciales(info.value) == 100

    asyncio.run(run())


def test_frozen_method_sin_seconds_es_admin_required() -> None:
    """P3 auditoría 3: FrozenMethodInvalidError (cuenta congelada) es el único
    FloodError SIN .seconds — int(exc.seconds) reventaba con AttributeError y
    el paso salía 'error inesperado'. Ahora mapea a AdminRequired (determinista,
    sin bucle caliente)."""
    from telethon.errors import FrozenMethodInvalidError

    async def run() -> None:
        client = FakeClient(raise_exc=FrozenMethodInvalidError(request=None))
        try:
            await execute_step(client, StepKind.BLOCK, _bot())
            raise AssertionError("debía traducir a AdminRequired")
        except AdminRequired as exc:
            assert "congelada" in str(exc)

    asyncio.run(run())
