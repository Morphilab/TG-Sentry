"""Tests del motor de ejecución con gateway falso. Sin red, sin Telegram."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tg_sentry.audit import AuditLog, StepOutcome
from tg_sentry.errors import (
    AdminRequired,
    FloodWait,
    PeerGone,
    PlanExpiredError,
    RpcFailure,
    TransientNetwork,
    UnsupportedStep,
)
from tg_sentry.executor import EngineLimits, ExecutionEngine
from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import PlannerOptions, StepKind, build_plan
from tg_sentry.runstate import step_key

PASO = tuple[int, str]  # (peer_id, step)


class FakeGateway:
    """Scriptable: mapa (peer_id, paso) → lista de excepciones; vacío/ausente = éxito."""

    def __init__(
        self, script: dict[tuple[int, str], list[Exception]] | None = None
    ) -> None:
        self.script = script or {}
        self.ejecutados: list[tuple[int, str]] = []

    async def run_step(self, step: StepKind, dialog: DialogInfo) -> None:
        key = (dialog.id, step.value)
        self.ejecutados.append(key)
        cola = self.script.get(key)
        if cola:
            raise cola.pop(0)


class SleepRecorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _plan(*kinds: PeerKind) -> object:
    dialogs = [
        DialogInfo(id=100 + index, kind=kind, name=f"peer-{kind.value}-{index}")
        for index, kind in enumerate(kinds)
    ]
    return build_plan(dialogs, PlannerOptions(ttl_min=30), me_id=999)


def _engine(
    tmp_path: Path,
    gateway: FakeGateway,
    *,
    limits: EngineLimits | None = None,
    audit: AuditLog | None = None,
    sleep: SleepRecorder | None = None,
) -> tuple[ExecutionEngine, SleepRecorder, AuditLog]:
    recorder = sleep or SleepRecorder()
    log = audit or AuditLog(tmp_path / "audit", rotate_bytes=10**9, keep_days=90)
    engine = ExecutionEngine(
        gateway,
        limits or EngineLimits(writes_per_min=1_000_000, backoff_base_s=0.001),
        log,
        sleep=recorder,
    )
    return engine, recorder, log


def test_camino_feliz_registra_auditoria() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.BOT, PeerKind.CHANNEL)
        gateway = FakeGateway()
        engine, _, log = _engine(tmp_root, gateway)
        pasos: list[tuple[int, str, StepOutcome]] = []
        result = await engine.run(
            plan, after_step=lambda p, s, o: pasos.append((p, s.value, o))
        )
        assert result.done == 3  # bot: delete+block · canal: leave
        assert result.aborted is None
        assert pasos == [
            (100, "delete_chat", StepOutcome.DONE),
            (100, "block", StepOutcome.DONE),
            (101, "leave", StepOutcome.DONE),
        ]
        registros = log.read()
        assert [r.outcome for r in registros] == [StepOutcome.DONE] * 3

    tmp_root = _tmp()
    asyncio.run(run())


def test_mapeo_peer_gone_a_estados_contractuales() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.CHANNEL)
        gateway = FakeGateway(
            {(100, "leave"): [PeerGone("not_participant")]}
        )
        engine, _, log = _engine(tmp_root, gateway)
        result = await engine.run(plan)
        assert result.skipped == {"skipped_ok": 1}
        assert log.read()[0].outcome is StepOutcome.SKIPPED_OK

        for razon, esperado in [
            ("channel_private", StepOutcome.SKIPPED_EXTERNAL),
            ("peer_invalid", StepOutcome.SKIPPED_UNREACHABLE),
            ("user_deactivated", StepOutcome.SKIPPED_EXTERNAL),
        ]:
            log2 = AuditLog(tmp_root / f"a-{razon}", rotate_bytes=10**9, keep_days=90)
            engine2 = ExecutionEngine(
                FakeGateway({(100, "leave"): [PeerGone(razon)]}),  # type: ignore[arg-type]
                EngineLimits(writes_per_min=1_000_000),
                log2,
                sleep=SleepRecorder(),
            )
            plan2 = _plan(PeerKind.CHANNEL)
            await engine2.run(plan2)
            assert log2.read()[0].outcome is esperado

    tmp_root = _tmp()
    asyncio.run(run())


def test_floodwait_pequeno_se_respeta_y_reintenta() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.CHANNEL)
        gateway = FakeGateway({(100, "leave"): [FloodWait(2)]})
        engine, recorder, _ = _engine(tmp_root, gateway)
        result = await engine.run(plan)
        assert result.done == 1
        assert 2.0 in recorder.sleeps  # se esperó lo que Telegram pidió

    tmp_root = _tmp()
    asyncio.run(run())


def test_floodwait_grande_aborta_la_cola() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.BOT, PeerKind.CHANNEL)
        gateway = FakeGateway({(100, "delete_chat"): [FloodWait(301)]})
        engine, _, log = _engine(tmp_root, gateway)
        result = await engine.run(plan)
        assert result.aborted is not None and "umbral" in result.aborted
        assert result.done == 0
        assert log.read() == []

    tmp_root = _tmp()
    asyncio.run(run())


def test_patron_de_baneo_dos_floodwait_en_ventana() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.CHANNEL, PeerKind.CHANNEL)
        gateway = FakeGateway(
            {
                (100, "leave"): [FloodWait(1)],
                (101, "leave"): [FloodWait(1)],
            }
        )
        engine, _, _ = _engine(tmp_root, gateway)
        result = await engine.run(plan)
        # el primer paso se recuperó; el segundo FloodWait disparó el patrón
        assert result.done == 1
        assert result.aborted is not None and "floodwait_pattern" in result.aborted

    tmp_root = _tmp()
    asyncio.run(run())


def test_red_transitoria_backoff_y_recuperacion() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.CHANNEL)
        gateway = FakeGateway({(100, "leave"): [TransientNetwork(), TransientNetwork()]})
        engine, recorder, _ = _engine(tmp_root, gateway)
        result = await engine.run(plan)
        assert result.done == 1
        # backoff 1,2 con base 0.001
        assert recorder.sleeps[:2] == [0.001, 0.002]

    tmp_root = _tmp()
    asyncio.run(run())


def test_red_transitoria_agota_reintentos_y_aborta() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.BOT, PeerKind.CHANNEL)
        gateway = FakeGateway({(100, "delete_chat"): [TransientNetwork()] * 10})
        limits = EngineLimits(
            writes_per_min=1_000_000, transient_retries=3, backoff_base_s=0.001
        )
        engine, recorder, _ = _engine(tmp_root, gateway, limits=limits)
        result = await engine.run(plan)
        assert result.aborted is not None and "network" in result.aborted
        # contrato documentado: backoff 1,2,4,8,16 — transient_retries=3
        # significa 3 REINTENTOS tras el intento inicial (auditoría 2026-09:
        # antes el 3.º fallo abortaba sin el último backoff del contrato)
        assert recorder.sleeps == [0.001, 0.002, 0.004]

    tmp_root = _tmp()
    asyncio.run(run())


class RelojFalso:
    """Reloj que avanza cuando el motor duerme: para testear ventanas rodantes."""

    def __init__(self) -> None:
        self._instante = datetime.now(UTC)

    def ahora(self) -> datetime:
        return self._instante

    async def dormir_avanzando(self, segundos: float) -> None:
        self._instante += timedelta(seconds=segundos)


def test_tope_hora_espera_ventana_y_continua() -> None:
    # M6.4: presupuesto agotado ya NO aborta — espera la ventana rodante
    async def run() -> None:
        from tg_sentry.audit import AuditRecord
        from tg_sentry.limits import MAX_DESTRUCTIVE_PER_HOUR

        reloj = RelojFalso()
        log = AuditLog(tmp_root / "audit", rotate_bytes=10**9, keep_days=90)
        for i in range(MAX_DESTRUCTIVE_PER_HOUR):
            log.record(
                AuditRecord(
                    ts=reloj.ahora() - timedelta(seconds=3500),  # sale en ~600s
                    plan_id="previo",
                    peer_id=i,
                    step="delete_chat",
                    outcome=StepOutcome.DONE,
                )
            )
        plan = _plan(PeerKind.CHANNEL)
        engine = ExecutionEngine(
            FakeGateway(),
            EngineLimits(writes_per_min=1_000_000),
            log,
            sleep=reloj.dormir_avanzando,
            clock=reloj.ahora,
        )
        result = await engine.run(plan)
        assert result.aborted is None  # esperó y siguió
        assert result.done == 1

    tmp_root = _tmp()
    asyncio.run(run())


def test_cap_por_tipo_espera_y_continua() -> None:
    # M6.4: el cap de tipo agotado espera la ventana en vez de abortar.
    # TTL holgado (2ª auditoría): la espera de presupuesto re-valida el TTL.
    async def run() -> None:
        reloj = RelojFalso()
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.CHANNEL, name="c1"),
            DialogInfo(id=101, kind=PeerKind.CHANNEL, name="c2"),
        ]
        plan = build_plan(dialogs, PlannerOptions(ttl_min=720), me_id=999)
        engine = ExecutionEngine(
            FakeGateway(),
            EngineLimits(writes_per_min=1_000_000, leave_per_hour=1),
            AuditLog(tmp_root / "audit", rotate_bytes=10**9, keep_days=90),
            sleep=reloj.dormir_avanzando,
            clock=reloj.ahora,
        )
        result = await engine.run(plan)
        assert result.aborted is None
        assert result.done == 2  # el 2º leave esperó su ventana y corrió

    tmp_root = _tmp()
    asyncio.run(run())


def test_tope_dia_espera_ventana_y_continua() -> None:
    # M6.4: 200/día agotado → espera larga (desatendido), no abort.
    # TTL holgado (2ª auditoría): la espera de presupuesto re-valida el TTL.
    async def run() -> None:
        from tg_sentry.audit import AuditRecord
        from tg_sentry.limits import MAX_DESTRUCTIVE_PER_DAY

        reloj = RelojFalso()
        log = AuditLog(tmp_root / "audit", rotate_bytes=10**9, keep_days=90)
        for i in range(MAX_DESTRUCTIVE_PER_DAY):
            log.record(
                AuditRecord(
                    ts=reloj.ahora() - timedelta(hours=23),  # sale en ~1h
                    plan_id="previo",
                    peer_id=i,
                    step="delete_chat",
                    outcome=StepOutcome.DONE,
                )
            )
        dialogs = [
            DialogInfo(id=100, kind=PeerKind.CHANNEL, name="canal")
        ]
        plan = build_plan(dialogs, PlannerOptions(ttl_min=720), me_id=999)
        sleeps: list[float] = []

        async def dormir_y_avanzar(segundos: float) -> None:
            sleeps.append(segundos)
            await reloj.dormir_avanzando(segundos)

        engine = ExecutionEngine(
            FakeGateway(),
            EngineLimits(writes_per_min=1_000_000),
            log,
            sleep=dormir_y_avanzar,
            clock=reloj.ahora,
        )
        result = await engine.run(plan)
        assert result.aborted is None
        assert result.done == 1
        assert max(sleeps) >= 3500  # esperó ~1 h, no abortó

    tmp_root = _tmp()
    asyncio.run(run())


def test_ttl_vencido_durante_espera_presupuesto_no_ejecuta() -> None:
    """2ª auditoría P2: la espera de presupuesto puede durar horas; el paso
    NO corre bajo un plan formalmente vencido (antes el último paso corría
    igual tras vencer el TTL)."""
    async def run() -> None:
        from tg_sentry.audit import AuditRecord
        from tg_sentry.limits import MAX_DESTRUCTIVE_PER_DAY

        reloj = RelojFalso()
        log = AuditLog(tmp_root / "audit", rotate_bytes=10**9, keep_days=90)
        for i in range(MAX_DESTRUCTIVE_PER_DAY):
            log.record(
                AuditRecord(
                    ts=reloj.ahora() - timedelta(hours=23),  # espera ~1h
                    plan_id="previo",
                    peer_id=i,
                    step="delete_chat",
                    outcome=StepOutcome.DONE,
                )
            )
        gateway = FakeGateway()
        dialogs = [DialogInfo(id=100, kind=PeerKind.CHANNEL, name="canal")]
        plan = build_plan(dialogs, PlannerOptions(ttl_min=30), me_id=999)
        engine = ExecutionEngine(
            gateway,
            EngineLimits(writes_per_min=1_000_000),
            log,
            sleep=reloj.dormir_avanzando,
            clock=reloj.ahora,
        )
        result = await engine.run(plan)
        assert result.aborted is not None and "ttl" in result.aborted
        assert result.done == 0
        assert gateway.ejecutados == []  # el paso jamás llegó al gateway

    tmp_root = _tmp()
    asyncio.run(run())


def test_plan_expirado_levanta_error() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.CHANNEL)
        expirado = plan.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(minutes=1)}
        )
        engine, _, _ = _engine(tmp_root, FakeGateway())
        with pytest.raises(PlanExpiredError):
            await engine.run(expirado)

    tmp_root = _tmp()
    asyncio.run(run())


def test_reanudacion_salta_pasos_ya_completados() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.BOT)
        gateway = FakeGateway()
        engine, _, _ = _engine(tmp_root, gateway)
        await engine.run(plan, completed={step_key(100, StepKind.DELETE_CHAT)})
        # solo ejecutó el block
        assert gateway.ejecutados == [(100, "block")]

    tmp_root = _tmp()
    asyncio.run(run())


def test_admin_required_y_unsupported_fallan_sin_abortar() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.BOT, PeerKind.CHANNEL)
        gateway = FakeGateway(
            {
                (100, "delete_chat"): [AdminRequired("no puedes")],
                (101, "leave"): [UnsupportedStep("pendiente")],
            }
        )
        engine, _, _ = _engine(tmp_root, gateway)
        result = await engine.run(plan)
        assert result.failed == 2
        assert result.aborted is None

    tmp_root = _tmp()
    asyncio.run(run())


def test_max_peers_limita_el_lote() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.CHANNEL, PeerKind.CHANNEL, PeerKind.CHANNEL)
        gateway = FakeGateway()
        engine, _, _ = _engine(tmp_root, gateway)
        result = await engine.run(plan, max_peers=2)
        assert result.done == 2
        assert result.aborted is None

    tmp_root = _tmp()
    asyncio.run(run())


def test_rpcfailure_reintenta_y_falla_el_item_sin_abortar() -> None:
    async def run() -> None:
        plan = _plan(PeerKind.BOT, PeerKind.CHANNEL)
        gateway = FakeGateway({(100, "delete_chat"): [RpcFailure("rpc")] * 10})
        limits = EngineLimits(
            writes_per_min=1_000_000, transient_retries=3, backoff_base_s=0.001
        )
        engine, _, _ = _engine(tmp_root, gateway, limits=limits)
        result = await engine.run(plan)
        assert result.aborted is None  # el lote SIGUE
        assert result.failed >= 1
        # el canal posterior sí se procesa
        assert any(p == 101 for p, _ in gateway.ejecutados)

    tmp_root = _tmp()
    asyncio.run(run())


def test_error_inesperado_no_tira_el_lote() -> None:
    # regresión del piloto: un AttributeError reventaba la corrida entera
    async def run() -> None:
        plan = _plan(PeerKind.BOT, PeerKind.CHANNEL)
        gateway = FakeGateway({(100, "block"): [RuntimeError("bang")]})
        engine, _, log = _engine(tmp_root, gateway)
        result = await engine.run(plan)
        assert result.aborted is None
        assert result.done >= 1  # el delete_chat del bot y el leave del canal
        assert result.failed == 1
        registro = next(r for r in log.read() if r.outcome is StepOutcome.FAILED)
        assert "error inesperado" in (registro.reason or "")

    tmp_root = _tmp()
    asyncio.run(run())


def _tmp() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="tgs-exec-"))


# --- Regresiones auditoría 2026-09 (rama api→motor) -------------------------


def test_sesion_invalida_aborta_sin_quemar_reintentos(tmp_path: Path) -> None:
    """Sesión revocada = terminal: aborta la cola; antes caía a RpcFailure y
    quemaba transient_retries por CADA ítem restante (auditoría 2026-09)."""
    from tg_sentry.errors import SessionInvalid

    gateway = FakeGateway(script={(100, "delete_chat"): [SessionInvalid("revocada")]})
    plan = _plan(PeerKind.BOT, PeerKind.BOT)
    engine, _recorder, _audit = _engine(tmp_path, gateway)
    resultado = asyncio.run(engine.run(plan))
    assert resultado.aborted is not None and "sesión inválida" in resultado.aborted
    # SOLO 1 intento del paso (no 5 reintentos) y el 2º peer ni se toca
    assert gateway.ejecutados == [(100, "delete_chat")]


def test_ttl_revalidado_a_mitad_de_corrida(tmp_path: Path) -> None:
    """P2: una corrida de horas no puede seguir bajo un plan vencido."""
    # el reloj avanza 6 min por CADA paso ejecutado: tras el 1º, el plan
    # (TTL 5 min) queda vencido y el 2º peer aborta por re-validación
    class RelojTTL:
        def __init__(self, gateway: FakeGateway) -> None:
            self.base = datetime.now(UTC)
            self.gateway = gateway

        def ahora(self) -> datetime:
            return self.base + timedelta(minutes=6 * len(self.gateway.ejecutados))

        async def dormir(self, seconds: float) -> None:
            pass

    async def run() -> None:
        gateway = FakeGateway()
        reloj = RelojTTL(gateway)
        plan = _plan(PeerKind.CHANNEL, PeerKind.CHANNEL)
        plan = plan.model_copy(
            update={
                "expires_at": reloj.base + timedelta(minutes=5),
                "created_at": reloj.base,
            }
        )
        engine = ExecutionEngine(
            gateway,
            EngineLimits(writes_per_min=1_000_000),
            AuditLog(tmp_path / "audit", rotate_bytes=10**9, keep_days=90),
            sleep=reloj.dormir,
            clock=reloj.ahora,
        )
        resultado = await engine.run(plan)
        assert resultado.aborted is not None and "ttl" in resultado.aborted
        assert resultado.done == 1  # el primer peer corrió; el 2º ya no
        assert len(gateway.ejecutados) == 1

    asyncio.run(run())


def test_fallo_persistencia_tras_paso_aborta_sin_lanzar(tmp_path: Path) -> None:
    """P2: un error de I/O en after_step (runstate lleno, permisos) respeta
    el contrato run() nunca lanza; el paso YA está auditado."""
    async def run() -> None:
        gateway = FakeGateway()
        plan = _plan(PeerKind.CHANNEL, PeerKind.CHANNEL)
        engine, _, audit = _engine(tmp_path, gateway)

        def after_step_roto(peer_id: int, paso: StepKind, resultado: StepOutcome) -> None:
            raise OSError("disco lleno")

        resultado = await engine.run(plan, after_step=after_step_roto)
        assert resultado.aborted is not None and "persistencia" in resultado.aborted
        # el primer paso SÍ quedó auditado (la auditoría va antes del after)
        registros = audit.read()
        assert len(registros) == 1 and registros[0].outcome is StepOutcome.DONE

    asyncio.run(run())


def test_rpc_agotado_con_borrado_parcial_aplica_cooldown(tmp_path: Path) -> None:
    """P3: un delete_own que borra lotes y acaba en fallo TAMBIÉN espacia
    (trabajo destructivo reciente sin cooldown era un hueco)."""
    async def run() -> None:
        class FallaConParcial(FakeGateway):
            async def run_step(self, step, dialog):  # type: ignore[no-untyped-def]
                self.ejecutados.append((dialog.id, step.value))
                exc = RpcFailure("boom")
                exc.tg_sentry_borrados_parciales = 100  # type: ignore[attr-defined]
                raise exc

        gateway = FallaConParcial()
        plan = build_plan(
            [
                DialogInfo(id=100 + i, kind=PeerKind.GROUP, name=f"g{i}")
                for i in range(2)
            ],
            PlannerOptions(delete_own="me", leave=False, ttl_min=30),
            me_id=999,
        )
        engine, recorder, _ = _engine(
            tmp_path,
            gateway,
            limits=EngineLimits(
                writes_per_min=1_000_000,
                transient_retries=1,
                backoff_base_s=0.001,
                own_messages_cooldown_s=1200,
            ),
        )
        resultado = await engine.run(plan)
        assert resultado.failed == 2
        # tras el 1er failed con 100 msgs parciales: cooldown 1200 registrado
        assert 1200 in recorder.sleeps

    asyncio.run(run())


def test_failed_sin_conteo_aplica_cooldown_conservador(tmp_path: Path) -> None:
    """2ª auditoría P3: un delete_own FAILED sin conteo conocido dejó
    trabajo desconocido en el servidor — cooldown conservador completo."""
    async def run() -> None:
        class FallaSinConteo(FakeGateway):
            async def run_step(self, step, dialog):  # type: ignore[no-untyped-def]
                self.ejecutados.append((dialog.id, step.value))
                raise RpcFailure("boom sin adjunto")

        gateway = FallaSinConteo()
        plan = build_plan(
            [
                DialogInfo(id=100 + i, kind=PeerKind.GROUP, name=f"g{i}")
                for i in range(2)
            ],
            PlannerOptions(delete_own="me", leave=False, ttl_min=30),
            me_id=999,
        )
        engine, recorder, _ = _engine(
            tmp_path,
            gateway,
            limits=EngineLimits(
                writes_per_min=1_000_000,
                transient_retries=1,
                backoff_base_s=0.001,
                own_messages_cooldown_s=900,
            ),
        )
        resultado = await engine.run(plan)
        assert resultado.failed == 2
        assert 900 in recorder.sleeps  # conservador completo (borrados None)

    asyncio.run(run())


def test_trabajo_real_failed_con_parcial_cuenta_para_presupuesto() -> None:
    """2ª auditoría P2: un FAILED con msgs=N>0 dejó trabajo real en el
    servidor — cuenta para el sobre (50/h·200/d); msgs=0 y FAILED sin
    conteo no."""
    from tg_sentry.audit import AuditRecord

    def rec(outcome: StepOutcome, reason: str | None) -> AuditRecord:
        return AuditRecord(
            ts=datetime.now(UTC), plan_id="p", peer_id=1, step="delete_own_for_me",
            outcome=outcome, reason=reason,
        )

    assert ExecutionEngine._trabajo_real(rec(StepOutcome.DONE, None))
    assert ExecutionEngine._trabajo_real(rec(StepOutcome.FAILED, "rpc_agotado: x; msgs=45"))
    assert not ExecutionEngine._trabajo_real(rec(StepOutcome.FAILED, "rpc_agotado: x; msgs=0"))
    assert not ExecutionEngine._trabajo_real(rec(StepOutcome.FAILED, "rpc_agotado: x"))
    assert not ExecutionEngine._trabajo_real(rec(StepOutcome.SKIPPED_OK, None))


def test_cap_delete_messages_gobierna_pasos_delete_own(tmp_path: Path) -> None:
    """P3: delete_messages_per_hour era config muerta; ahora caps los pasos
    de borrado propio (espera rodante, no abort)."""
    async def run() -> None:
        reloj = RelojFalso()
        plan = build_plan(
            [
                DialogInfo(id=100 + i, kind=PeerKind.GROUP, name=f"g{i}")
                for i in range(3)
            ],
            # el reloj falso avanza horas en las esperas del cap: TTL holgado
            PlannerOptions(delete_own="me", ttl_min=720),
            me_id=999,
        )
        gateway = FakeGateway()
        engine = ExecutionEngine(
            gateway,
            EngineLimits(
                writes_per_min=1_000_000,
                delete_messages_per_hour=2,
                own_messages_cooldown_s=0,
            ),
            AuditLog(tmp_path / "audit", rotate_bytes=10**9, keep_days=90),
            sleep=reloj.dormir_avanzando,
            clock=reloj.ahora,
        )
        resultado = await engine.run(plan)
        assert resultado.aborted is None
        # 3 grupos x (delete_own + leave) = 6 pasos: el delete_own 3.º esperó
        # su ventana del cap y corrió igual (espera rodante, no abort)
        assert resultado.done == 6

    asyncio.run(run())
