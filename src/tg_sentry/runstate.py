"""Estado persistente de ejecución por plan (reanudación tras corte).

Un archivo JSON por plan bajo ~/.local/state/tg-sentry/runs/: guarda el plan
congelado y el conjunto de pasos ya completados (peer_id, step). `apply`
carga el estado si existe y reanuda exactamente lo pendiente — los pasos ya
auditados no se re-ejecutan.

Robustez:
- El tmp de escritura lleva el PID: dos procesos (CLI + TUI) no pisan el
  mismo tmp ni instalan JSON corrupto con replace.
- Carga FAIL-CLOSED: un runstate corrupto NO se ignora en silencio (re-
  ejecutaría pasos done); exige revisión y apunta a la auditoría.
- `ejecucion_exclusiva`: lock GLOBAL + por plan (flock) — solo UNA ejecución
  viva en todo el sistema (CLI o TUI), y el mismo plan jamás dos veces.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import tg_sentry.paths as paths
from tg_sentry.planner import ActionPlan, StepKind

CompletedStep = tuple[int, str]  # (peer_id, step_kind.value)

try:  # flock es POSIX; en otro SO la exclusión no está disponible
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# plan_id legítimo = uuid4().hex (lo genera build_plan). Un plan editado a
# mano con separadores de ruta NO debe poder escribir fuera de runs_dir/
# (defensa en profundidad).
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class RunstateCorrupto(Exception):
    """El runstate no se puede leer: fail-closed, no se ignora en silencio."""


def runs_dir() -> Path:
    return paths.state_dir() / "runs"


def _validar_plan_id(plan_id: str) -> str:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise RunstateCorrupto(
            f"plan_id inválido ({plan_id!r}): no tiene el formato generado por "
            "build_plan (32 hex). El plan puede estar manipulado o corrupto."
        )
    return plan_id


def state_path(plan: ActionPlan) -> Path:
    _validar_plan_id(plan.plan_id)
    return runs_dir() / f"plan-{plan.plan_id}.json"


def save(plan: ActionPlan, completed: set[CompletedStep]) -> Path:
    target = state_path(plan)
    # ensure_private_dir (no mkdir crudo): ancestros nuevos a 0700
    # (convención de paths.py)
    paths.ensure_private_dir(target.parent)
    payload = {
        "plan": json.loads(plan.model_dump_json()),
        "completed": sorted([peer_id, step] for peer_id, step in completed),
    }
    # tmp con PID: colisiones entre procesos producían JSON corrupto.
    # fsync ANTES del replace (robustez): un corte de luz dejaba el
    # target vacío/parcial y la reanudación fail-closed.
    tmp = target.with_suffix(f".tmp-{os.getpid()}")
    # 0600 DESDE LA CREACIÓN (robustez: open("w")+chmod final dejaba el
    # runstate — lleva el plan completo con nombres de diálogos — visible a
    # 0644 para siempre si había crash entre replace y chmod)
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=1))
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(target)
    paths.fsync_dir(target.parent)
    target.chmod(0o600)  # repara además un runstate legacy preexistente holgado
    return target


def load_completed(plan: ActionPlan) -> set[CompletedStep]:
    file = state_path(plan)
    if not file.exists():
        return set()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):  # JSON válido pero no-objeto
            raise ValueError("la raíz del runstate no es un objeto JSON")
        completed = data.get("completed", [])
        if not isinstance(completed, list):
            raise ValueError("'completed' no es una lista")
        return {(int(peer_id), str(step)) for peer_id, step in completed}
    except (ValueError, TypeError, OSError, AttributeError) as exc:
        raise RunstateCorrupto(
            f"el estado del plan {plan.plan_id} está corrupto ({exc}). "
            "ANTES de borrarlo revisa la auditoría (tg-sentry audit): los pasos "
            "done constan ahí y no deben re-ejecutarse a ciegas."
        ) from exc


@contextlib.contextmanager
def ejecucion_exclusiva(plan: ActionPlan) -> Iterator[bool]:
    """Lock no-bloqueante GLOBAL + por plan. Cede True si se obtuvo; False si
    ya hay OTRA ejecución viva en cualquier proceso (cualquier plan — CLI y
    TUI) o del MISMO plan.

    El lock global existe por robustez: el lock por plan solo impedía correr el
    MISMO plan dos veces; dos planes distintos en paralelo (CLI + TUI)
    duplicaban el ritmo del token bucket (8/min) y en el borde exacto del
    tope podían superarlo en 1-2 ops. El bucket es memoria por-proceso; los
    topes duros se respetan porque la auditoría es compartida, pero el ritmo
    de arranque se duplica. Orden de adquisición fijo (global → plan): sin
    deadlock posible; LOCK_NB: el perdedor no espera.
    """
    if fcntl is None:  # pragma: no cover — SO no-POSIX: sin lock disponible
        yield True
        return
    _validar_plan_id(plan.plan_id)  # plan_id manipulado no escribe fuera de runs/
    # ensure_private_dir (no mkdir crudo): ancestros nuevos a 0700
    # (convención de paths.py)
    paths.ensure_private_dir(runs_dir())
    global_handle = open(  # noqa: SIM115 — los handles viven hasta el finally
        runs_dir() / "ejecucion-global.lock", "w"
    )
    plan_handle = open(  # noqa: SIM115
        runs_dir() / f"plan-{plan.plan_id}.lock", "w"
    )
    global_ok = False
    plan_ok = False
    try:
        try:
            fcntl.flock(global_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            global_ok = True
            fcntl.flock(plan_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            plan_ok = True
        except OSError:
            obtenido = False
        else:
            obtenido = True
        yield obtenido
    finally:
        # desbloquear SOLO lo adquirido: si el plan lock falla con el global
        # tomado, el global debe liberarse igualmente (otra ejecución legitima
        # debe poder arrancar)
        for ok, handle in ((plan_ok, plan_handle), (global_ok, global_handle)):
            if ok:
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def step_key(peer_id: int, step: StepKind) -> CompletedStep:
    return (peer_id, step.value)
