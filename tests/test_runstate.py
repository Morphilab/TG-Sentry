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
    """2ª auditoría: JSON válido pero no-objeto ("[1,2]") reventaba con
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
    """2ª auditoría: un plan manipulado con plan_id tipo ../x NO debe poder
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
