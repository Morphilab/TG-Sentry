"""Tests del planificador: matriz de pasos, exclusiones y TTL del plan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.test_inventory import _to_infos
from tg_sentry.models import PeerKind
from tg_sentry.planner import (
    ActionPlan,
    PlannerOptions,
    StepKind,
    blindados_en_plan,
    build_plan,
)


def _options(**kwargs) -> PlannerOptions:
    return PlannerOptions(**kwargs)


def test_matriz_bot_bloquear_y_eliminar() -> None:
    bot = [d for d in _to_infos() if d.kind is PeerKind.BOT]
    plan = build_plan(bot, _options(), me_id=1)
    assert len(plan.peers) == 1
    pasos = [s.kind for s in plan.peers[0].steps]
    assert pasos == [StepKind.DELETE_CHAT, StepKind.BLOCK]


def test_matriz_bot_sin_bloquear() -> None:
    bot = [d for d in _to_infos() if d.kind is PeerKind.BOT]
    plan = build_plan(bot, _options(block_bots=False), me_id=1)
    assert [s.kind for s in plan.peers[0].steps] == [StepKind.DELETE_CHAT]


def test_matriz_grupo_y_canal() -> None:
    dialogs = [d for d in _to_infos() if d.kind in {PeerKind.GROUP, PeerKind.CHANNEL}]
    plan = build_plan(dialogs, _options(), me_id=1)
    por_id = {p.dialog.id: [s.kind for s in p.steps] for p in plan.peers}
    assert por_id[-100333] == [StepKind.LEAVE]  # grupo
    assert por_id[-100444] == [StepKind.LEAVE]  # canal
    # con vaciado previo, el grupo gana un paso antes de salir
    plan2 = build_plan(dialogs, _options(clear_history_before_leave=True), me_id=1)
    por_id2 = {p.dialog.id: [s.kind for s in p.steps] for p in plan2.peers}
    assert por_id2[-100333] == [StepKind.CLEAR_HISTORY, StepKind.LEAVE]
    assert por_id2[-100444] == [StepKind.LEAVE]  # canal no cambia


def test_matriz_privado_variants() -> None:
    user = [d for d in _to_infos() if d.kind is PeerKind.USER]
    assert [s.kind for s in build_plan(user, _options(), me_id=1).peers[0].steps] == [
        StepKind.DELETE_CHAT
    ]
    assert [
        s.kind for s in build_plan(user, _options(revoke_private=True), me_id=1).peers[0].steps
    ] == [StepKind.DELETE_CHAT_REVOKE]
    assert [
        s.kind
        for s in build_plan(user, _options(block_private=True), me_id=1).peers[0].steps
    ] == [StepKind.DELETE_CHAT, StepKind.BLOCK]


def test_excluidos_quedan_listados_con_razon() -> None:
    dialogs = _to_infos()  # incluye 555 (unknown)
    plan = build_plan(dialogs, _options(), me_id=1)
    excluidos = dict(plan.excluded)
    assert 555 in excluidos and "desconocido" in excluidos[555]
    assert 111 not in excluidos


def test_plan_congelado_ttl() -> None:
    plan = build_plan([], _options(ttl_min=5), me_id=1)
    assert isinstance(plan, ActionPlan)
    assert not plan.is_expired()
    futuro = datetime.now(UTC) + timedelta(minutes=6)
    assert plan.is_expired(now=futuro)


def test_contadores_destructivos() -> None:
    dialogs = [d for d in _to_infos() if d.kind in {PeerKind.BOT, PeerKind.CHANNEL}]
    plan = build_plan(dialogs, _options(), me_id=1)
    # bot: 2 pasos; canales: 1 paso c/u (2 canales) → 4 destructivas
    assert plan.destructive_count() == 4
    assert plan.counts_by_step() == {
        "delete_chat": 1, "block": 1, "leave": 2,
    }


# --- Regresiones de la auditoría 2026-09 -----------------------------------


def test_leave_false_en_usuario_sin_borrado_propio_excluye() -> None:
    """P1: --no-leave sin delete_own NO puede borrar el chat privado igual."""
    user = [d for d in _to_infos() if d.kind is PeerKind.USER]
    plan = build_plan(user, _options(leave=False), me_id=1)
    assert plan.peers == []  # nada aplicable → excluido, jamás delete_chat
    assert any("sin pasos" in razon for _, razon in plan.excluded)


def test_leave_false_en_bot_excluye() -> None:
    """P1: leave=False en bots se respeta (antes: delete_chat igual)."""
    bot = [d for d in _to_infos() if d.kind is PeerKind.BOT]
    plan = build_plan(bot, _options(leave=False), me_id=1)
    assert plan.peers == []
    assert any("sin pasos" in razon for _, razon in plan.excluded)


def test_leave_false_solo_limpia_mensajes_propios() -> None:
    user = [d for d in _to_infos() if d.kind is PeerKind.USER]
    plan = build_plan(user, _options(leave=False, delete_own="me"), me_id=1)
    assert [s.kind for s in plan.peers[0].steps] == [StepKind.DELETE_OWN_FOR_ME]


def test_bot_ahora_respeta_delete_own_antes_de_borrar_chat() -> None:
    """delete_own también aplica a bots, SIEMPRE antes de salir/block."""
    bot = [d for d in _to_infos() if d.kind is PeerKind.BOT]
    plan = build_plan(bot, _options(delete_own="me"), me_id=1)
    assert [s.kind for s in plan.peers[0].steps] == [
        StepKind.DELETE_OWN_FOR_ME,
        StepKind.DELETE_CHAT,
        StepKind.BLOCK,
    ]


def test_orden_grupo_delete_own_antes_de_clear_history() -> None:
    """Si clear_history existiera, no puede anular el borrado de propios."""
    grupos = [d for d in _to_infos() if d.kind is PeerKind.GROUP]
    plan = build_plan(
        grupos, _options(delete_own="me", clear_history_before_leave=True), me_id=1
    )
    assert [s.kind for s in plan.peers[0].steps] == [
        StepKind.DELETE_OWN_FOR_ME,
        StepKind.CLEAR_HISTORY,
        StepKind.LEAVE,
    ]


def test_build_plan_sin_me_id_rechaza() -> None:
    """me_id es obligatorio: fail-closed (auditoría 2026-09)."""
    import pytest

    with pytest.raises(ValueError, match="me_id"):
        build_plan(_to_infos(), _options(), me_id=None)  # type: ignore[arg-type]


def test_plan_lleva_me_id_para_revalidacion() -> None:
    plan = build_plan(_to_infos(), _options(), me_id=1)
    assert plan.me_id == 1


def test_blindados_en_plan_detecta_infiltrado() -> None:
    """Defensa en profundidad de apply: un plan con un blindado NO se ejecuta."""
    from tg_sentry.safety import hard_exclusions  # noqa: F401

    plan = build_plan([d for d in _to_infos() if d.kind is PeerKind.BOT], _options(), me_id=1)
    assert blindados_en_plan(plan, me_id=1) == []
    # inyectamos un peer blindado (777000) en el plan como si una versión con
    # bug lo hubiera colado
    from tg_sentry.models import DialogInfo
    from tg_sentry.planner import PlannedPeer, PlanStep

    infiltrado = PlannedPeer(
        dialog=DialogInfo(id=777000, kind=PeerKind.USER, name="Telegram"),
        steps=[PlanStep(kind=StepKind.DELETE_CHAT)],
    )
    plan2 = plan.model_copy(deep=True)
    plan2.peers.append(infiltrado)
    hallados = blindados_en_plan(plan2, me_id=1)
    assert len(hallados) == 1 and hallados[0][0] == 777000


def test_blindados_en_plan_detecta_otra_identidad() -> None:
    plan = build_plan(_to_infos(), _options(), me_id=1)
    hallados = blindados_en_plan(plan, me_id=999)
    assert hallados and "OTRA identidad" in hallados[0][1]


def test_canal_respeta_delete_own_antes_de_salir() -> None:
    """Regresión (auditoría 2026-09, P1): leave=True (default) jamás descarta
    el paso de mensajes propios — salir sin borrar destruye la única ventana
    para borrarlos (quien abandona ya no puede borrar nada del canal)."""
    canales = [d for d in _to_infos() if d.kind is PeerKind.CHANNEL]
    plan = build_plan(canales, _options(delete_own="me"), me_id=1)
    assert [s.kind for s in plan.peers[0].steps] == [
        StepKind.DELETE_OWN_FOR_ME, StepKind.LEAVE,
    ]
    plan2 = build_plan(canales, _options(delete_own="all"), me_id=1)
    assert [s.kind for s in plan2.peers[0].steps] == [
        StepKind.DELETE_OWN_FOR_ALL, StepKind.LEAVE,
    ]
    # sin salir ni borrar: nada aplicable (igual que antes)
    plan3 = build_plan(canales, _options(leave=False), me_id=1)
    assert plan3.peers == []


def test_grupo_clear_history_solo_al_salir() -> None:
    """leave=False promete conservar el chat: clear_history no se planifica."""
    grupos = [d for d in _to_infos() if d.kind is PeerKind.GROUP]
    plan = build_plan(
        grupos,
        _options(delete_own="me", leave=False, clear_history_before_leave=True),
        me_id=1,
    )
    assert [s.kind for s in plan.peers[0].steps] == [StepKind.DELETE_OWN_FOR_ME]


def test_blindados_en_plan_rechaza_plan_sin_me_id() -> None:
    """Fail-closed: un plan cuyo JSON no lleva me_id (antiguo o editado a mano)
    NO pasa la re-validación de identidad (auditoría 2026-09, P2)."""
    plan = build_plan(_to_infos(), _options(), me_id=1)
    sin_me = plan.model_copy(deep=True, update={"me_id": None})
    hallados = blindados_en_plan(sin_me, me_id=1)
    assert hallados and "me_id" in hallados[0][1]
