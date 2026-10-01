"""Estado persistente de ejecución por plan (reanudación tras corte).

Un archivo JSON por plan bajo ~/.local/state/tg-sentry/runs/: guarda el plan
congelado y el conjunto de pasos ya completados (peer_id, step). `apply`
carga el estado si existe y reanuda exactamente lo pendiente — los pasos ya
auditados no se re-ejecutan.

Robustez (auditoría 2026-09):
- El tmp de escritura lleva el PID: dos procesos (CLI + TUI) no pisan el
  mismo tmp ni instalan JSON corrupto con replace.
- Carga FAIL-CLOSED: un runstate corrupto NO se ignora en silencio (re-
  ejecutaría pasos done); exige revisión y apunta a la auditoría.
- `ejecucion_exclusiva`: lock por plan (flock) — CLI y TUI no pueden correr
  el mismo plan a la vez.
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
# (2ª auditoría).
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    payload = {
        "plan": json.loads(plan.model_dump_json()),
        "completed": sorted([peer_id, step] for peer_id, step in completed),
    }
    # tmp con PID: colisiones entre procesos producían JSON corrupto.
    # fsync ANTES del replace (2ª auditoría): un corte de luz dejaba el
    # target vacío/parcial y la reanudación fail-closed.
    tmp = target.with_suffix(f".tmp-{os.getpid()}")
    # 0600 DESDE LA CREACIÓN (3ª auditoría: open("w")+chmod final dejaba el
    # runstate — lleva el plan completo con nombres de diálogos — visible a
    # 0644 para siempre si había crash entre replace y chmod)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    """Lock no-bloqueante por plan. cede True si se obtuvo; False si ya hay
    otra ejecución viva del MISMO plan (CLI y TUI, dos procesos)."""
    if fcntl is None:  # pragma: no cover — SO no-POSIX: sin lock disponible
        yield True
        return
    _validar_plan_id(plan.plan_id)  # plan_id manipulado no escribe fuera de runs/
    runs_dir().mkdir(parents=True, exist_ok=True)
    runs_dir().chmod(0o700)
    handle = open(  # noqa: SIM115 — el handle vive hasta el finally
        runs_dir() / f"plan-{plan.plan_id}.lock", "w"
    )
    obtenido = False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            obtenido = True
        except OSError:
            obtenido = False
        yield obtenido
    finally:
        if obtenido:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def step_key(peer_id: int, step: StepKind) -> CompletedStep:
    return (peer_id, step.value)
