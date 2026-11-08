"""Tests del estado persistente de ejecución. Sin red."""

from __future__ import annotations

from pathlib import Path

import pytest

from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.planner import PlannerOptions, StepKind, build_plan
from tg_sentry.runstate import CompletedStep, load_completed, save, step_key


def _plan() -> object:
    dialogs = [DialogInfo(id=7, kind=PeerKind.BOT, name="b")]
    return build_plan(dialogs, PlannerOptions(ttl_min=5), me_id=999)


def test_save_y_load_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    plan = _plan()
    assert load_completed(plan) == set()  # type: ignore[arg-type]
    completed: set[CompletedStep] = {step_key(7, StepKind.DELETE_CHAT)}
    target = save(plan, completed)  # type: ignore[arg-type]
    assert target.exists()
    assert load_completed(plan) == completed  # type: ignore[arg-type]
    assert target.parent.name == "runs"


def test_step_key_formato() -> None:
    assert step_key(42, StepKind.BLOCK) == (42, "block")


def test_load_runstate_raiz_no_objeto_es_corrupto(tmp_path: Path) -> None:
    """JSON válido pero no-objeto ("[1,2]") fallaba con
    AttributeError en vez del fail-closed accionable."""
    import json as _json

    from tg_sentry.models import DialogInfo, PeerKind
    from tg_sentry.planner import PlannerOptions, build_plan
    from tg_sentry.runstate import RunstateCorrupto, state_path

    plan = build_plan(
        [DialogInfo(id=1, kind=PeerKind.BOT, name="b")],
        PlannerOptions(), me_id=9,
    )
    file = state_path(plan)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(_json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(RunstateCorrupto):
        load_completed(plan)


def test_plan_id_con_separadores_de_ruta_rechazado() -> None:
    """un plan manipulado con plan_id tipo ../x NO debe poder
    escribir fuera de runs_dir/."""
    from tg_sentry.models import DialogInfo, PeerKind
    from tg_sentry.planner import PlannerOptions, build_plan
    from tg_sentry.runstate import RunstateCorrupto, state_path

    plan = build_plan(
        [DialogInfo(id=1, kind=PeerKind.BOT, name="b")],
        PlannerOptions(), me_id=9,
    )
    manipulado = plan.model_copy(update={"plan_id": "../../x"})
    with pytest.raises(RunstateCorrupto):
        state_path(manipulado)


def test_lock_global_impide_dos_planes_en_paralelo(tmp_path: Path, monkeypatch) -> None:
    """el lock era SOLO por plan — CLI y TUI podían correr dos
    planes DISTINTOS a la vez y duplicar el ritmo efectivo del token bucket
    (8/min). Ahora solo UNA ejecución viva en todo el sistema."""
    from tg_sentry.runstate import ejecucion_exclusiva

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    plan_a = _plan()
    plan_b = build_plan(
        [DialogInfo(id=9, kind=PeerKind.BOT, name="b2")],
        PlannerOptions(ttl_min=5),
        me_id=999,
    )
    with ejecucion_exclusiva(plan_a) as exclusivo_a:  # type: ignore[arg-type]
        assert exclusivo_a
        # OTRO plan: bloqueado por el lock GLOBAL (no solo por el de plan)
        with ejecucion_exclusiva(plan_b) as exclusivo_b:  # type: ignore[arg-type]
            assert not exclusivo_b
        # y el MISMO plan, por el lock de plan
        with ejecucion_exclusiva(plan_a) as mismo:  # type: ignore[arg-type]
            assert not mismo
    # liberado A: B vuelve a poder ejecutarse
    with ejecucion_exclusiva(plan_b) as exclusivo_b2:  # type: ignore[arg-type]
        assert exclusivo_b2


def test_lock_global_se_libera_al_fallar_el_de_plan(tmp_path: Path, monkeypatch) -> None:
    """Si la adquisición falla a mitad (global tomado, plan no), el global se
    desbloquea en el finally: otra ejecución legítima debe poder arrancar."""
    import fcntl

    from tg_sentry import runstate as rs
    from tg_sentry.runstate import ejecucion_exclusiva

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    plan = _plan()
    rs.runs_dir().mkdir(parents=True, exist_ok=True)
    # alguien ajeno retiene el lock del plan (p. ej. proceso caído con handle)
    ajeno = open(rs.runs_dir() / f"plan-{plan.plan_id}.lock", "w")  # noqa: SIM115
    fcntl.flock(ajeno, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with ejecucion_exclusiva(plan) as primero:  # type: ignore[arg-type]
        assert not primero
    # el global NO debe quedar retenido por el intento fallido: si lo
    # estuviera, este segundo intento también fallaría por el GLOBAL y no
    # por el lock del plan ajeno
    with ejecucion_exclusiva(plan) as segundo:  # type: ignore[arg-type]
        assert not segundo  # sigue bloqueado por el ajeno (lock de plan)
    fcntl.flock(ajeno, fcntl.LOCK_UN)
    ajeno.close()
    with ejecucion_exclusiva(plan) as tercero:  # type: ignore[arg-type]
        assert tercero
