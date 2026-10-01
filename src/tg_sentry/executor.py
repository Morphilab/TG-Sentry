"""Motor de ejecución: consume el plan congelado, paso a paso, con límites.

El motor NO importa Telethon: ejecuta pasos vía un gateway (TeleOpsGateway en
producción, fakes en tests) y consume la taxonomía de `tg_sentry.errors`.

Disciplina aplicada antes de cada paso:
1. Topes duros de sesión (50/h, 200/día) contados desde la auditoría.
2. Caps por tipo de operación (leave/block por hora).
3. Token bucket (~4/min default; solo configurable a la baja).

Durante el paso:
- FloodWait manda: se respeta la espera (bucket pausado y vaciado, gracia
  2x) y se reintenta el paso. Espera > umbral → abort de la cola. Segundo
  FloodWait en la ventana → patrón de baneo → abort (reanudación manual).
- Fallo transitorio de red: backoff exponencial 1,2,4,8,16s
  (transient_retries reintentos tras el primer intento), luego abort
  (los pasos quedan pendientes y reanudables).
- Sesión revocada: terminal → abort inmediato (re-hacer login).
- Resultados por paso → auditoría inmediata con estado contractual.
- El TTL del plan se re-valida por CADA PEER: una corrida de horas con un
  plan de 5 min no puede seguir operando bajo un plan formalmente vencido.

Abort ≠ error: el estado persistente permite reanudar lo pendiente.
`run()` NUNCA lanza por un fallo del entorno: un error de I/O en la
persistencia (runstate/auditoría) se convierte en abort con motivo.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from tg_sentry.audit import AuditLog, AuditRecord, StepOutcome
from tg_sentry.errors import (
    AdminRequired,
    FloodWait,
    PeerGone,
    PlanExpiredError,
    RpcFailure,
    SessionInvalid,
    TransientNetwork,
    UnsupportedStep,
)
from tg_sentry.limits import MAX_DESTRUCTIVE_PER_DAY, MAX_DESTRUCTIVE_PER_HOUR
from tg_sentry.models import DialogInfo
from tg_sentry.planner import ActionPlan, PlannedPeer, StepKind
from tg_sentry.ratelimit import FloodWaitMonitor, TokenBucket

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]
AfterStepFn = Callable[[int, StepKind, StepOutcome], None]

# Pasos que borran hasta ~100 mensajes en un request interno: merecen
# descanso propio entre pasos (el bucket solo controla 1 op/step).
OWN_MESSAGE_STEPS = frozenset({StepKind.DELETE_OWN_FOR_ALL, StepKind.DELETE_OWN_FOR_ME})

GONE_TO_OUTCOME = {
    "not_participant": StepOutcome.SKIPPED_OK,
    "channel_private": StepOutcome.SKIPPED_EXTERNAL,
    "peer_invalid": StepOutcome.SKIPPED_UNREACHABLE,
    "user_deactivated": StepOutcome.SKIPPED_EXTERNAL,
}


class EngineLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    writes_per_min: int = Field(default=4, ge=1)
    leave_per_hour: int = Field(default=30, ge=1)
    block_per_hour: int = Field(default=50, ge=1)
    delete_messages_per_hour: int = Field(default=100, ge=1)
    floodwait_pause_threshold_s: int = Field(default=300, ge=1)
    floodwait_pattern_window_s: int = Field(default=300, ge=1)
    # ge=0 (no el piso 300 de producto): el piso lo valida config.Limits al
    # cargar; aquí 0 permite a los tests ejercitar la omisión del cooldown
    own_messages_cooldown_s: int = Field(default=1200, ge=0)
    transient_retries: int = Field(default=5, ge=1)
    backoff_base_s: float = Field(default=1.0, gt=0)


@dataclass
class RunResult:
    done: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    aborted: str | None = None
    messages_deleted: int = 0

    @property
    def total_processed(self) -> int:
        return self.done + sum(self.skipped.values()) + self.failed


class _AbortRun(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ExecutionEngine:
    def __init__(
        self,
        gateway: object,
        limits: EngineLimits,
        audit: AuditLog,
        *,
        bucket: TokenBucket | None = None,
        monitor: FloodWaitMonitor | None = None,
        sleep: SleepFn = asyncio.sleep,
        sleep_budget: SleepFn | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._limits = limits
        self._audit = audit
        self._bucket = bucket if bucket is not None else TokenBucket(limits.writes_per_min)
        self._monitor = monitor if monitor is not None else FloodWaitMonitor()
        self._sleep = sleep
        self._sleep_budget = sleep_budget if sleep_budget is not None else sleep
        # reloj inyectable: imprescindible para TESTEAR ventanas rodantes
        # sin esperar horas reales (el sleep de test lo avanza)
        self._now = clock if clock is not None else (lambda: datetime.now(UTC))

    async def run(
        self,
        plan: ActionPlan,
        *,
        completed: set[tuple[int, str]] | None = None,
        after_step: AfterStepFn | None = None,
        max_peers: int | None = None,
    ) -> RunResult:
        """Ejecuta los pasos pendientes del plan. Nunca lanza por un paso."""
        if plan.is_expired(self._now()):
            raise PlanExpiredError(
                f"plan {plan.plan_id} expiró (TTL): regénéralo y confirma de nuevo"
            )
        done_steps = completed if completed is not None else set()
        result = RunResult()
        peers = plan.peers if max_peers is None else plan.peers[:max_peers]
        # lista plana de tareas pendientes: permite saber si queda algún paso
        # de borrado propio (el cooldown final es cola muerta — hallazgo M6.3)
        tareas = [
            (peer, step, step.kind in OWN_MESSAGE_STEPS)
            for peer in peers
            for step in peer.steps
            if (peer.dialog.id, step.kind.value) not in done_steps
        ]
        try:
            for indice, (peer, step, es_borrado) in enumerate(tareas):
                if plan.is_expired(self._now()):
                    # re-validación a mitad de corrida: el TTL es la promesa
                    # de re-validación contra el estado remoto — una corrida
                    # de horas no puede seguir bajo un plan vencido
                    raise _AbortRun(
                        f"ttl: el plan venció a mitad de corrida "
                        f"({len(tareas) - indice} pasos quedaban); "
                        "regenera el plan y reanuda"
                    )
                while True:
                    espera = self._espera_presupuesto(step.kind)
                    if espera is None:
                        break
                    # visible también en CLI (el log es la única traza ahí);
                    # la TUI anuncia lo mismo por su canal propio
                    logger.info(
                        "presupuesto anti-baneo lleno: esperando %.0fs antes "
                        "de %s (ventana rodante)",
                        espera,
                        step.kind.value,
                    )
                    await self._sleep_budget(espera)
                await self._bucket.acquire(wait=self._sleep)
                if plan.is_expired(self._now()):
                    # el paso NO se ejecuta bajo un plan vencido: la espera de
                    # presupuesto puede durar horas (tope 200/día) y el TTL es
                    # la promesa de re-validación contra el estado remoto
                    # (2ª auditoría: antes el último paso corría vencido igual)
                    raise _AbortRun(
                        "ttl: el plan venció durante la espera de presupuesto; "
                        "regenera el plan y reanuda"
                    )
                try:
                    outcome, reason, borrados = await self._execute_step(
                        peer, step.kind
                    )
                    self._record(
                        plan.plan_id, peer.dialog, step.kind, outcome, reason, borrados
                    )
                except _AbortRun:
                    raise
                except Exception as exc:
                    # auditoría/ES caída (OSError o un registro imposible de
                    # persistir): NO continuar contando sobre un sobre de
                    # seguridad que ya no podemos registrar
                    raise _AbortRun(f"auditoría: {exc}") from exc
                self._bump(result, outcome)
                if borrados is not None:
                    result.messages_deleted += borrados
                if after_step is not None:
                    try:
                        after_step(peer.dialog.id, step.kind, outcome)
                    except Exception as exc:
                        # el paso YA está auditado; un fallo de persistencia
                        # del runstate/UX no puede romper el contrato
                        # "run() nunca lanza" (auditoría 2026-09)
                        raise _AbortRun(
                            f"persistencia tras el paso: {exc}"
                        ) from exc
                if es_borrado and outcome in (StepOutcome.DONE, StepOutcome.FAILED):
                    # Cooldown PROPORCIONAL a lo borrado (hallazgo M6.2: el
                    # fijo de 20 min castigaba igual a un grupo con 90
                    # mensajes y otro con 3). Presupuesto: 1 msg cada
                    # cooldown/100 segundos → ~300 msgs/h con el default.
                    # Piso 60s; sin conteo (borrados None), conservador
                    # completo — TAMBIÉN en FAILED (2ª auditoría: un fallo
                    # sin conteo conocido dejó trabajo desconocido en el
                    # servidor). Se OMITE si no se borró nada o si no queda
                    # ningún paso de borrado por delante (cola muerta, M6.3).
                    if borrados == 0:
                        continue
                    quedan_borrados = any(
                        es for _, _, es in tareas[indice + 1 :]
                    )
                    cooldown = self._cooldown_por_borrado(borrados)
                    if cooldown > 0 and quedan_borrados:
                        await self._sleep(cooldown)
        except _AbortRun as abort:
            result.aborted = abort.reason
        return result

    def _cooldown_por_borrado(self, borrados: int | None) -> float:
        """Cooldown post-borrado proporcional: piso 60s, techo el configurado.

        borrados == 0 → 0 (nada borrado, nada que espaciar); None →
        conservador completo (gateway que no reporta conteo).
        """
        if borrados is None:
            return float(self._limits.own_messages_cooldown_s)  # conservador
        if borrados == 0:
            return 0.0
        por_mensaje = self._limits.own_messages_cooldown_s / 100.0
        return min(
            float(self._limits.own_messages_cooldown_s),
            max(60.0, borrados * por_mensaje),
        )

    # -- pasos -------------------------------------------------------------

    async def _execute_step(
        self, peer: PlannedPeer, step: StepKind
    ) -> tuple[StepOutcome, str | None, int | None]:
        """Devuelve (resultado, razón, mensajes borrados — solo pasos de propios).

        En fallos de pasos DELETE_OWN_* se recupera el conteo PARCIAL que la
        capa tg/ adjunta a la excepción (`tg_sentry_borrados_parciales`):
        trabajo destructivo ya hecho debe espaciarse aunque el ítem acabe
        en failed (auditoría 2026-09).
        """
        attempt = 0
        while True:
            try:
                borrados = await self._gateway.run_step(step, peer.dialog)  # type: ignore[attr-defined]
                # bool es int en Python: un gateway que devuelva True no
                # audita msgs=True (3ª auditoría, nit)
                if not isinstance(borrados, int) or isinstance(borrados, bool):
                    borrados = None  # gateway que no reporta conteo
                return StepOutcome.DONE, None, borrados
            except FloodWait as flood:
                self._monitor.record()
                if self._monitor.pattern_detected(self._limits.floodwait_pattern_window_s):
                    raise _AbortRun(
                        "floodwait_pattern: pausa indefinida (reanudación manual)"
                    ) from flood
                if flood.seconds > self._limits.floodwait_pause_threshold_s:
                    raise _AbortRun(
                        f"floodwait:{flood.seconds}s supera el umbral "
                        f"{self._limits.floodwait_pause_threshold_s}s"
                    ) from flood
                self._bucket.floodwait_pause(flood.seconds)
                await self._sleep(flood.seconds)
                continue  # se respeta la espera y se reintenta el MISMO paso
            except TransientNetwork as net:
                attempt += 1
                if attempt > self._limits.transient_retries:
                    raise _AbortRun("network: backoff agotado, cola pausada") from net
                await self._sleep(self._limits.backoff_base_s * 2 ** (attempt - 1))
                continue
            except RpcFailure as rpc:
                # RPC genérico: reintentos y luego fallo del ÍTEM (sin abortar lote)
                attempt += 1
                if attempt > self._limits.transient_retries:
                    return (
                        StepOutcome.FAILED,
                        f"rpc_agotado: {rpc}",
                        self._borrados_parciales(rpc),
                    )
                await self._sleep(self._limits.backoff_base_s * 2 ** (attempt - 1))
                continue
            except PeerGone as gone:
                # .get con fallback FAILED: una razón nueva en errors.py sin
                # actualizar el mapa no revienta run() (2ª auditoría)
                return GONE_TO_OUTCOME.get(gone.reason, StepOutcome.FAILED), (
                    gone.reason
                ), None
            except AdminRequired as exc:
                return StepOutcome.FAILED, f"admin_required: {exc}", None
            except UnsupportedStep as exc:
                return StepOutcome.FAILED, str(exc), None
            except SessionInvalid as exc:
                # sesión revocada en el servidor: TERMINAL — reintentar solo
                # quemaría el presupuesto de cada ítem restante (auditoría
                # 2026-09: antes caía a RpcFailure y quemaba 5 reintentos x
                # ítem). La cola aborta; el camino es re-hacer login.
                raise _AbortRun(f"sesión inválida: re-haz login ({exc})") from exc
            except Exception as exc:
                # Un bug inesperado NO tira la corrida: el paso queda failed
                # con la traza completa en el log, y el lote continúa.
                logger.exception(
                    "error inesperado en paso %s del peer %s", step.value, peer.dialog.id
                )
                return (
                    StepOutcome.FAILED,
                    f"error inesperado: {type(exc).__name__}: {exc}",
                    self._borrados_parciales(exc),
                )

    @staticmethod
    def _borrados_parciales(exc: BaseException) -> int | None:
        # el conteo parcial viaja adjunto a la EXCEPCIÓN ORIGINAL de Telethon;
        # la traducción de tg/ops.py la re-lanza con `from` — se recorre la
        # cadena __cause__/__context__ (2ª auditoría: en producción el
        # atributo moría en la traducción y el cooldown era código muerto)
        actual: BaseException | None = exc
        vistas: set[int] = set()
        while actual is not None and id(actual) not in vistas:
            vistas.add(id(actual))
            parcial = getattr(actual, "tg_sentry_borrados_parciales", None)
            if isinstance(parcial, int):
                return parcial
            actual = actual.__cause__ or actual.__context__
        return None

    # -- presupuestos: ESPERAR la ventana, nunca abortar (hallazgo M6.4) -----
    # El tope agotado se traduce en espera hasta que el registro done más
    # viejo DENTRO de la ventana salga de ella (ventana rodante): el sobre
    # de seguridad se mantiene idéntico (jamás >50/h ni >200/d), pero los
    # trabajos grandes corren desatendidos en vez de abortar cada hora.

    def _espera_presupuesto(self, step: StepKind) -> float | None:
        """Segundos hasta que haya presupuesto para este paso; None = hay."""
        hechos = [
            (r.ts, r.step)
            for r in self._audit.read()
            if self._trabajo_real(r)
        ]
        ahora = self._now()
        esperas: list[float | None] = [
            self._espera_si_tope(
                [t for t, _ in hechos if t >= ahora - timedelta(hours=1)],
                MAX_DESTRUCTIVE_PER_HOUR,
                timedelta(hours=1),
            ),
            self._espera_si_tope(
                [t for t, _ in hechos if t >= ahora - timedelta(days=1)],
                MAX_DESTRUCTIVE_PER_DAY,
                timedelta(days=1),
            ),
        ]
        caps: dict[StepKind, int] = {
            StepKind.LEAVE: self._limits.leave_per_hour,
            StepKind.BLOCK: self._limits.block_per_hour,
            # el cap de mensajes (config) gobierna los pasos que borran
            # mensajes: antes era config muerta (auditoría 2026-09)
            StepKind.DELETE_OWN_FOR_ALL: self._limits.delete_messages_per_hour,
            StepKind.DELETE_OWN_FOR_ME: self._limits.delete_messages_per_hour,
        }
        if step in caps:
            cap_propio = caps[step]
            esperas.append(
                self._espera_si_tope(
                    [
                        t
                        for t, paso in hechos
                        if paso == step.value and t >= ahora - timedelta(hours=1)
                    ],
                    cap_propio,
                    timedelta(hours=1),
                )
            )
        pendientes = [e for e in esperas if e is not None]
        return max(pendientes) if pendientes else None

    def _espera_si_tope(
        self, marcas: list[datetime], tope: int, ventana: timedelta
    ) -> float | None:
        if len(marcas) < tope:
            return None
        salida = min(marcas) + ventana  # el más viejo abandona la ventana aquí
        espera = (salida - self._now()).total_seconds() + 2  # margen
        return max(1.0, espera)

    @staticmethod
    def _trabajo_real(r: AuditRecord) -> bool:
        """DONE cuenta siempre; FAILED solo si dejó trabajo parcial en el
        servidor (el conteo viaja en reason como `msgs=N`, N>0 — 2ª auditoría:
        un delete_own que borró 100 y falló contaba 0 para el sobre)."""
        if r.outcome is StepOutcome.DONE:
            return True
        if r.outcome is not StepOutcome.FAILED or not r.reason:
            return False
        m = re.search(r"msgs=(\d+)", r.reason)
        return bool(m and int(m.group(1)) > 0)

    # -- auditoría ----------------------------------------------------------

    def _record(
        self,
        plan_id: str,
        dialog: DialogInfo,
        step: StepKind,
        outcome: StepOutcome,
        reason: str | None,
        borrados: int | None = None,
    ) -> None:
        if borrados is not None:
            conteo = f"msgs={borrados}"
            reason = f"{reason}; {conteo}" if reason else conteo
        self._audit.record(
            AuditRecord(
                ts=self._now(),
                plan_id=plan_id,
                peer_id=dialog.id,
                step=step.value,
                outcome=outcome,
                reason=reason,
            )
        )

    @staticmethod
    def _bump(result: RunResult, outcome: StepOutcome) -> None:
        if outcome is StepOutcome.DONE:
            result.done += 1
        elif outcome is StepOutcome.FAILED:
            result.failed += 1
        else:
            result.skipped[outcome.value] = result.skipped.get(outcome.value, 0) + 1
