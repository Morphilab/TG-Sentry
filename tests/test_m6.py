"""Tests M6: mensajes propios (dos modos 48h), cooldown del motor, export audit."""

from __future__ import annotations

import asyncio
import csv
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon.tl.types import InputPeerChannel, InputPeerChat

from tests.test_executor import FakeGateway, _engine
from tests.test_inventory import _to_infos
from tg_sentry.audit import AuditLog, AuditRecord, StepOutcome, record_outcome
from tg_sentry.errors import FloodWait, UnsupportedStep
from tg_sentry.executor import EngineLimits
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import PlannerOptions, StepKind, build_plan
from tg_sentry.tg.ops import TeleOpsGateway, delete_own_messages


class _Msg:
    def __init__(self, ident: int) -> None:
        self.id = ident



class _OwnClient:
    """iter_messages + delete_messages registrados, sin red."""

    def __init__(self, n_mensajes: int = 250) -> None:
        self.delete_calls: list[tuple[int, list[int], bool]] = []
        self._n = n_mensajes

    async def get_input_entity(self, peer: int):  # type: ignore[no-untyped-def]
        # como la sesión REAL: el peer resuelve a InputPeerChat (grupo básico,
        # donde revoke=False sí es "solo para ti")
        return InputPeerChat(chat_id=abs(peer))

    def iter_messages(self, peer: int, *, from_user: str, limit: int, reverse: bool = False):  # type: ignore[no-untyped-def]
        assert from_user == "me"

        async def _gen():
            # la API REAL itera de más NUEVO a más viejo salvo reverse=True
            # (auditoría 2026-09: el fake antiguo asumía ascendente y ocultaba
            # que el código no pedía reverse)
            if reverse:
                for i in range(min(self._n, limit)):
                    yield _Msg(1000 + i)  # 1000, 1001…: del más viejo al más nuevo
            else:
                for i in range(min(self._n, limit)):
                    yield _Msg(9000 - i)  # descendente: el más nuevo primero

        return _gen()

    async def delete_messages(self, peer: int, ids: list[int], *, revoke: bool):  # type: ignore[no-untyped-def]
        self.delete_calls.append((peer, ids, revoke))
        return None  # como la API real sin pts_count legible en el fake


def test_delete_own_me_lotes_de_100_sin_revoke() -> None:
    async def run() -> None:
        client = _OwnClient(n_mensajes=250)
        borrados = await delete_own_messages(client, 77, for_all=False, max_messages=250)
        assert borrados == 250
        assert [c[2] for c in client.delete_calls] == [False, False, False]
        assert [len(c[1]) for c in client.delete_calls] == [100, 100, 50]

    asyncio.run(run())


def test_delete_own_all_con_revoke() -> None:
    async def run() -> None:
        client = _OwnClient(n_mensajes=120)
        borrados = await delete_own_messages(client, 88, for_all=True, max_messages=120)
        assert borrados == 120
        assert [c[2] for c in client.delete_calls] == [True, True]

    asyncio.run(run())


def test_delete_own_respetando_max_messages() -> None:
    async def run() -> None:
        client = _OwnClient(n_mensajes=500)
        borrados = await delete_own_messages(client, 66, for_all=False, max_messages=100)
        assert borrados == 100
        assert len(client.delete_calls) == 1

    asyncio.run(run())


def test_planner_mensajes_propios_antes_de_salir() -> None:
    grupos = [d for d in _to_infos() if d.kind is PeerKind.GROUP]
    plan = build_plan(grupos, PlannerOptions(delete_own="me"), me_id=1)
    assert [s.kind for s in plan.peers[0].steps] == [
        StepKind.DELETE_OWN_FOR_ME, StepKind.LEAVE,
    ]
    plan2 = build_plan(grupos, PlannerOptions(delete_own="all"), me_id=1)
    assert [s.kind for s in plan2.peers[0].steps] == [
        StepKind.DELETE_OWN_FOR_ALL, StepKind.LEAVE,
    ]


def test_planner_sin_salir_solo_mensajes() -> None:
    grupos = [d for d in _to_infos() if d.kind is PeerKind.GROUP]
    plan = build_plan(grupos, PlannerOptions(delete_own="me", leave=False), me_id=1)
    assert [s.kind for s in plan.peers[0].steps] == [StepKind.DELETE_OWN_FOR_ME]
    canales = [d for d in _to_infos() if d.kind is PeerKind.CHANNEL]
    plan2 = build_plan(canales, PlannerOptions(leave=False), me_id=1)
    assert plan2.peers == []
    assert all("sin pasos aplicables" in p[1] for p in plan2.excluded)


def test_motor_cooldown_tras_paso_de_mensajes() -> None:
    async def run() -> None:
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.GROUP, name="g1"),
            DialogInfo(id=101, kind=PeerKind.GROUP, name="g2"),
        ]
        plan = build_plan(
            dialogs, PlannerOptions(delete_own="me", leave=False), me_id=1
        )
        gateway = FakeGateway()
        limits = EngineLimits(writes_per_min=1_000_000, own_messages_cooldown_s=25)
        engine, recorder, _ = _engine(
            Path("/tmp/tgs-m6-test"), gateway, limits=limits
        )
        result = await engine.run(plan)
        assert result.done == 2
        assert 25.0 in recorder.sleeps  # cooldown aplicado tras el 1er paso


def test_export_audit_csv_y_json(tmp_path: Path) -> None:
    from tg_sentry.__main__ import _export_audit

    audit = AuditLog(tmp_path / "audit", rotate_bytes=10**9, keep_days=90)
    audit.record(
        AuditRecord(
            ts=datetime.now(UTC),
            plan_id="p",
            peer_id=1,
            step="delete_chat",
            outcome=StepOutcome.DONE,
        )
    )
    registros = audit.read()

    destino = tmp_path / "a.csv"
    _export_audit(registros, destino, "csv")
    with destino.open(encoding="utf-8") as handle:
        filas = list(csv.DictReader(handle))
    assert len(filas) == 1 and filas[0]["outcome"] == "done"
    assert stat.S_IMODE(destino.stat().st_mode) == 0o600

    destino2 = tmp_path / "a.json"
    _export_audit(registros, destino2, "json")
    datos = json.loads(destino2.read_text(encoding="utf-8"))
    assert datos[0]["peer_id"] == 1


class _GatewayConConteo:
    """Gateway que reporta mensajes borrados (como BridgedGateway real)."""

    def __init__(self, conteo: int | None) -> None:
        self.conteo = conteo

    async def run_step(self, step, dialog) -> int | None:  # type: ignore[no-untyped-def]
        return self.conteo


def test_cooldown_proporcional_a_mensajes_borrados() -> None:
    async def run() -> None:
        # 2 grupos: tras el 1º SÍ hay cooldown (queda otro paso de borrado)
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.GROUP, name="g1"),
            DialogInfo(id=101, kind=PeerKind.GROUP, name="g2"),
        ]
        plan = build_plan(dialogs, PlannerOptions(delete_own="me", leave=False), me_id=1)
        gateway = _GatewayConConteo(10)  # 10 msgs → 10 x 12s = 120s (no 1200)
        limits = EngineLimits(writes_per_min=1_000_000, own_messages_cooldown_s=1200)
        engine, recorder, _ = _engine(Path("/tmp/tgs-m6b"), gateway, limits=limits)
        await engine.run(plan)
        assert 120.0 in recorder.sleeps

    asyncio.run(run())


def test_cooldown_piso_60s_cuando_borro_poco_o_nada() -> None:
    async def run() -> None:
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.GROUP, name="g1"),
            DialogInfo(id=101, kind=PeerKind.GROUP, name="g2"),
        ]
        plan = build_plan(dialogs, PlannerOptions(delete_own="me", leave=False), me_id=1)
        gateway = _GatewayConConteo(2)  # 2 x 12 = 24s → piso 60s
        limits = EngineLimits(writes_per_min=1_000_000, own_messages_cooldown_s=1200)
        engine, recorder, _ = _engine(Path("/tmp/tgs-m6c"), gateway, limits=limits)
        await engine.run(plan)
        assert 60.0 in recorder.sleeps

    asyncio.run(run())


def test_cooldown_conservador_si_gateway_no_reporta_conteo() -> None:
    async def run() -> None:
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.GROUP, name="g1"),
            DialogInfo(id=101, kind=PeerKind.GROUP, name="g2"),
        ]
        plan = build_plan(dialogs, PlannerOptions(delete_own="me", leave=False), me_id=1)
        gateway = _GatewayConConteo(None)
        limits = EngineLimits(writes_per_min=1_000_000, own_messages_cooldown_s=1200)
        engine, recorder, _ = _engine(Path("/tmp/tgs-m6d"), gateway, limits=limits)
        await engine.run(plan)
        assert 1200.0 in recorder.sleeps

    asyncio.run(run())


def test_runresult_suma_mensajes_y_auditoria_registra_msgs_n(tmp_path: Path) -> None:
    async def run() -> None:
        dialogs = [DialogInfo(id=100, kind=PeerKind.GROUP, name="g")]
        plan = build_plan(dialogs, PlannerOptions(delete_own="me", leave=False), me_id=1)
        audit = AuditLog(tmp_path / "audit", rotate_bytes=10**9, keep_days=90)
        gateway = _GatewayConConteo(37)
        limits = EngineLimits(writes_per_min=1_000_000, own_messages_cooldown_s=1200)
        engine, _, _ = _engine(tmp_path, gateway, limits=limits, audit=audit)
        result = await engine.run(plan)
        assert result.messages_deleted == 37
        registro = audit.read()[0]
        assert registro.reason == "msgs=37"

    asyncio.run(run())


def test_cooldown_omitido_si_nada_borrado() -> None:
    """M6.3: borrados=0 → nada que espaciar, cooldown 0."""
    async def run() -> None:
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.GROUP, name="g1"),
            DialogInfo(id=101, kind=PeerKind.GROUP, name="g2"),
        ]
        plan = build_plan(dialogs, PlannerOptions(delete_own="me", leave=False), me_id=1)
        gateway = _GatewayConConteo(0)
        limits = EngineLimits(writes_per_min=1_000_000, own_messages_cooldown_s=1200)
        engine, recorder, _ = _engine(Path("/tmp/tgs-m6e"), gateway, limits=limits)
        await engine.run(plan)
        assert not [s for s in recorder.sleeps if s >= 60]

    asyncio.run(run())


def test_cooldown_final_omitido_sin_mas_borrados() -> None:
    """M6.3: el ÚLTIMO paso de borrado no duerme cooldown (cola muerta)."""
    async def run() -> None:
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.GROUP, name="g1"),
            DialogInfo(id=101, kind=PeerKind.GROUP, name="g2"),
        ]
        plan = build_plan(dialogs, PlannerOptions(delete_own="me", leave=False), me_id=1)
        gateway = _GatewayConConteo(50)  # 50 x 12 = 600s c/u
        limits = EngineLimits(writes_per_min=1_000_000, own_messages_cooldown_s=1200)
        engine, recorder, _ = _engine(Path("/tmp/tgs-m6f"), gateway, limits=limits)
        await engine.run(plan)
        # solo UN cooldown (tras el 1º); tras el último no hay cola
        assert recorder.sleeps.count(600.0) == 1

    asyncio.run(run())


# --- Regresiones auditoría 2026-09 (rama api) -------------------------------


def test_delete_own_itera_del_mas_viejo_al_mas_nuevo() -> None:
    """La API itera más-nuevo→más-viejo por defecto; el paso exige reverse:
    lo primero que se borra debe ser lo MÁS VIEJO (contrato del docstring)."""

    async def run() -> None:
        client = _OwnClient(n_mensajes=250)
        await delete_own_messages(client, 77, for_all=False, max_messages=250)
        primer_lote = client.delete_calls[0][1]
        assert primer_lote == sorted(primer_lote)  # ascendente = viejos primero
        assert primer_lote[0] == 1000

    asyncio.run(run())


def test_delete_own_fallo_parcial_adjunta_conteo() -> None:
    """Un fallo a mitad de lotes lleva el conteo parcial adjunto: el motor
    podrá aplicar cooldown aunque el paso acabe en failed."""

    async def run() -> None:
        client = _OwnClient(n_mensajes=250)
        llamadas = 0
        original = client.delete_messages

        async def falla_en_segunda(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            nonlocal llamadas
            llamadas += 1
            if llamadas == 2:
                raise FloodWait(10)
            return await original(*args, **kwargs)

        client.delete_messages = falla_en_segunda  # type: ignore[method-assign]
        try:
            await delete_own_messages(client, 5, for_all=False, max_messages=250)
            raise AssertionError("debía fallar")
        except FloodWait as exc:
            assert getattr(exc, "tg_sentry_borrados_parciales", None) == 100

    asyncio.run(run())


def test_gateway_teleops_propaga_conteo() -> None:
    """El gateway REAL debe devolver el conteo: sin él el cooldown proporcional
    era código muerto en producción (auditoría 2026-09, P2)."""

    async def run() -> None:
        client = _OwnClient(n_mensajes=120)  # el paso acota a 100 por defecto
        gateway = TeleOpsGateway(client)
        count = await gateway.run_step(StepKind.DELETE_OWN_FOR_ME, _peer(88))
        assert count == 100  # PROPAGADO (antes el gateway lo descartaba)

    asyncio.run(run())


def _peer(pid: int):  # type: ignore[no-untyped-def]
    from tg_sentry.models import DialogInfo, PeerKind

    return DialogInfo(id=pid, kind=PeerKind.GROUP, name="g")


def test_export_audit_nace_0600_desde_la_creacion(tmp_path: Path) -> None:
    """2ª auditoría P1: el export de auditoría (peer_ids + razones) nacía con
    umask (0644) y el chmod llegaba al final — un crash dejaba los datos
    personales world-readable PARA SIEMPRE. Misma cura que el export de
    inventario: os.open con 0600 en la creación."""
    import os as _os
    from unittest.mock import patch

    from tg_sentry.__main__ import _export_audit

    log = AuditLog(tmp_path / "audit", rotate_bytes=10**9, keep_days=90)
    record_outcome("p", 1, "delete_chat", StepOutcome.DONE, audit=log)
    registros = log.read()

    modos: list[int] = []
    original = _os.open

    def espiando(path, flags, mode=0o777, *args, **kwargs):  # type: ignore[no-untyped-def]
        modos.append(mode)
        return original(path, flags, mode, *args, **kwargs)

    destino = tmp_path / "auditoria.csv"
    with patch("tg_sentry.export.os.open", side_effect=espiando):
        _export_audit(registros, destino, "csv")
    assert 0o600 in modos
    assert destino.exists()


def test_export_audit_csv_neutraliza_formulas(tmp_path: Path) -> None:
    """2ª auditoría: reason puede traer texto de terceros (mensajes de
    excepción); el CSV de auditoría no neutralizaba fórmulas."""
    from tg_sentry.__main__ import _export_audit

    log = AuditLog(tmp_path / "audit", rotate_bytes=10**9, keep_days=90)
    record_outcome("p", 1, "delete_chat", StepOutcome.FAILED, "=HIPERVINCULO()", audit=log)
    destino = tmp_path / "auditoria.csv"
    _export_audit(log.read(), destino, "csv")
    contenido = destino.read_text(encoding="utf-8")
    assert "'=HIPERVINCULO()" in contenido


def test_export_audit_default_lleva_microsegundos(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """2ª auditoría: dos exports del mismo formato en el mismo segundo se
    truncaban entre sí en silencio (open 'w' trunca)."""
    import re

    from tg_sentry import paths
    from tg_sentry.__main__ import _cmd_audit

    monkeypatch.setattr(paths, "exports_dir", lambda: tmp_path)
    _cmd_audit(None, "csv", None)
    nombres = [f.name for f in tmp_path.glob("auditoria-*.csv")]
    assert nombres, "no se creó el export"
    assert all(re.fullmatch(r"auditoria-\d{8}-\d{6}-\d+\.csv", n) for n in nombres)


def test_delete_own_for_me_rechazado_en_entidad_canal() -> None:
    """P1 auditoría 3: Telethon enruta canales/SUPERGRUPOS a
    channels.DeleteMessagesRequest SIN revoke — 'solo para ti' borraría para
    todos. El guard lo rechaza ANTES de tocar la API."""
    async def run() -> None:
        class _ClienteCanal(_OwnClient):
            async def get_input_entity(self, peer: int):  # type: ignore[no-untyped-def]
                return InputPeerChannel(channel_id=abs(peer), access_hash=1)

        client = _ClienteCanal(n_mensajes=50)
        with pytest.raises(UnsupportedStep):
            await delete_own_messages(client, -100123, for_all=False)
        assert client.delete_calls == []  # jamás llegó a borrar nada
        # for_all=True en canal: legítimo (borrar para todos ES la semántica real)
        client2 = _ClienteCanal(n_mensajes=50)
        borrados = await delete_own_messages(client2, -100123, for_all=True)
        assert borrados == 50 and len(client2.delete_calls) == 1

    asyncio.run(run())


def test_delete_own_conteo_real_pts_count() -> None:
    """El conteo REAL es pts_count del servidor (2ª auditoría): el fake
    antiguo devolvía dicts y solo ejercitaba el fallback len(lote)."""
    async def run() -> None:
        class _Resp:
            def __init__(self, n: int) -> None:
                self.pts_count = n

        class _ClientePts(_OwnClient):
            async def delete_messages(self, peer: int, ids: list[int], *, revoke: bool):  # type: ignore[no-untyped-def]
                self.delete_calls.append((peer, ids, revoke))
                return [_Resp(len(ids) // 2)]  # la mitad real de cada lote

        client = _ClientePts(n_mensajes=250)
        borrados = await delete_own_messages(client, 77, for_all=False, max_messages=250)
        assert borrados == 125  # (50 + 50 + 25) vía pts_count, NO el fallback 250

    asyncio.run(run())
