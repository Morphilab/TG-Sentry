"""Planificador: selección → plan congelado con pasos ordenados por tipo.

El plan es INMUTABLE una vez creado y lleva TTL (`expires_at`): el ejecutor
debe re-validarlo antes de aplicar. Matriz de pasos (semánticas verificadas
contra la API real):

- BOT:     [delete_own] → delete_chat → block (si block_bots)
- USER:    [delete_own] → delete_chat (± revoke si revoke_private)
             → block (si block_private)
- GROUP:   delete_own → clear_history (opcional) → leave
- CHANNEL: [delete_own] → leave. delete_own="me" NO se planifica en
  canales ('solo para ti' es imposible ahí): el peer queda excluido con
  razón — emitir el paso condenado y salir destruiría la única ventana
  de borrado.
- SECRET / UNKNOWN: jamás se planifican (safety los excluye).

`leave=False` se respeta en TODOS los tipos: solo limpia mensajes propios
(sin pasos de salir/borrar chat) o excluye si no hay nada que borrar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tg_sentry.models import DialogInfo, PeerKind
from tg_sentry.safety import hard_exclusions, parse_whitelist, soft_warnings


class StepKind(StrEnum):
    DELETE_CHAT = "delete_chat"            # elimina el diálogo (solo para ti)
    DELETE_CHAT_REVOKE = "delete_chat_revoke"  # privados: borra también del otro lado
    LEAVE = "leave"                        # grupos/canales: salir / darse de baja
    BLOCK = "block"                        # bloquear usuario/bot
    CLEAR_HISTORY = "clear_history"        # vaciar historial local antes de salir
    DELETE_OWN_FOR_ALL = "delete_own_for_all"  # propios: para todos (≤48h; luego degrada)
    DELETE_OWN_FOR_ME = "delete_own_for_me"    # propios: solo para ti (sin límite de edad)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: StepKind
    detail: str = ""


class PlannedPeer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialog: DialogInfo
    steps: list[PlanStep]
    warnings: list[str] = Field(default_factory=list)


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    created_at: datetime
    expires_at: datetime
    based_on_cache_fresh: bool
    peers: list[PlannedPeer]
    excluded: list[tuple[int, str]] = Field(default_factory=list)  # (peer_id, razón)
    # Identidad con la que se construyó el plan: apply re-valida contra ella
    # (defensa en profundidad).
    me_id: int | None = None

    def _expiracion_aware(self) -> datetime:
        """expires_at normalizado a UTC: un plan JSON editado a mano con ts
        naive reventaba la comparación con TypeError (robustez)."""
        if self.expires_at.tzinfo is None:
            return self.expires_at.replace(tzinfo=UTC)
        return self.expires_at

    def is_expired(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return reference >= self._expiracion_aware()

    def restante_ttl(self, now: datetime | None = None) -> timedelta:
        """Segundos hasta vencer (0 si ya venció): la UI muestra el TTL
        RESTANTE, no el de config (robustez)."""
        reference = now or datetime.now(UTC)
        return max(timedelta(0), self._expiracion_aware() - reference)

    def destructive_count(self) -> int:
        return sum(len(peer.steps) for peer in self.peers)

    def counts_by_step(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for peer in self.peers:
            for step in peer.steps:
                counts[step.kind.value] = counts.get(step.kind.value, 0) + 1
        return counts


class PlannerOptions(BaseModel):
    """Opciones por tipo aplicadas al construir el plan.

    delete_own: None = no tocar mensajes propios; "all" = para todos donde
    el servidor lo permita (≤48h); "me" = solo para ti (siempre posible
    EXCEPTO en canales/supergrupos, donde la API borra siempre para todos).
    leave: False = solo limpia mensajes propios SIN salir (chats conservados).
    """

    model_config = ConfigDict(extra="forbid")

    block_bots: bool = True
    block_private: bool = False
    revoke_private: bool = False
    clear_history_before_leave: bool = False
    delete_own: Literal[None, "all", "me"] = None
    leave: bool = True
    ttl_min: int = Field(default=5, ge=1)
    based_on_cache_fresh: bool = True


# Razón de exclusión de canales con delete_own="me": imposible por API
# (la excepción megagrupo — kind=GROUP — no se puede detectar aquí sin el
# flag megagroup en el inventario; el motor la cubre omitiendo los pasos
# que cortan el acceso tras un borrado fallido — ver executor).
_CANAL_SOLO_PARA_TI = (
    "canales no admiten borrar propios 'solo para ti': usa 'all' o desmarca salir"
)

# Exclusiones que NO son blindaje (el peer no es aplicable con estas
# OPCIONES): la UI las separa de los 🔒 blindados — "N blindados" contaba
# también los "sin pasos aplicables" y erosionaba la señal (robustez).
RAZONES_NO_APLICABLES = frozenset(
    {"sin pasos aplicables al tipo", _CANAL_SOLO_PARA_TI}
)


def build_plan(
    dialogs: list[DialogInfo],
    options: PlannerOptions,
    *,
    me_id: int,
    whitelist: list[str] | None = None,
    contact_ids: set[int] | None = None,
    protect_contacts: bool = True,
    recent_private_days: int = 30,
) -> ActionPlan:
    """Selección → plan congelado. Los excluidos quedan listados con razón.

    `me_id` es OBLIGATORIO (fail-closed): sin identidad propia verificada no
    hay plan — la blindadura de la propia cuenta no admite un None silencioso
    (hallazgo de robustez).
    """
    if me_id is None:  # defensa en runtime: el tipo ya lo exige
        msg = (
            "me_id requerido: refresca el inventario (dialogs --refresh) "
            "para guardar la identidad de tu cuenta"
        )
        raise ValueError(msg)
    whitelist_ids, whitelist_usernames = parse_whitelist(whitelist or [])
    now = datetime.now(UTC)

    peers: list[PlannedPeer] = []
    excluded: list[tuple[int, str]] = []
    for dialog in dialogs:
        reasons = hard_exclusions(
            dialog,
            me_id=me_id,
            whitelist_ids=whitelist_ids,
            whitelist_usernames=whitelist_usernames,
            contact_ids=contact_ids,
            protect_contacts=protect_contacts,
        )
        if reasons:
            excluded.append((dialog.id, "; ".join(reasons)))
            continue
        if dialog.kind is PeerKind.CHANNEL and options.delete_own == "me":
            # robustez: 'solo para ti' es IMPOSIBLE en canales (la API
            # borra SIEMPRE para todos — guard de tg/ops). Emitir el paso
            # condenado y ejecutar el leave tras su fallo destruía la única
            # ventana de borrado. El peer queda excluido con razón accionable.
            excluded.append((dialog.id, _CANAL_SOLO_PARA_TI))
            continue
        steps = _steps_for(dialog, options)
        if not steps:
            excluded.append((dialog.id, "sin pasos aplicables al tipo"))
            continue
        peers.append(
            PlannedPeer(
                dialog=dialog,
                steps=steps,
                warnings=soft_warnings(
                    dialog,
                    recent_private_days=recent_private_days,
                    now=now,
                ),
            )
        )

    return ActionPlan(
        plan_id=uuid4().hex,
        created_at=now,
        expires_at=now + timedelta(minutes=options.ttl_min),
        based_on_cache_fresh=options.based_on_cache_fresh,
        peers=peers,
        excluded=excluded,
        me_id=me_id,
    )


def blindados_en_plan(
    plan: ActionPlan,
    *,
    me_id: int,
    whitelist: list[str] | None = None,
    contact_ids: set[int] | None = None,
    protect_contacts: bool = True,
) -> list[tuple[int, str]]:
    """Re-valida las exclusiones duras sobre un plan ya construido.

    Defensa en profundidad para apply: un plan generado por una versión con
    bug, editado a mano o con identidad distinta NO se ejecuta (robustez).
    Devuelve (peer_id, razones) de todo peer blindado.
    """
    whitelist_ids, whitelist_usernames = parse_whitelist(whitelist or [])
    encontrados: list[tuple[int, str]] = []
    # Fail-closed: un plan SIN me_id (JSON antiguo o editado a mano) NO pasa
    # la re-validación de identidad — lo obligatorio se exige, lo ausente se
    # rechaza (robustez).
    if plan.me_id != me_id:
        return [
            (peer.dialog.id, "el plan fue generado por OTRA identidad (me_id distinto)")
            for peer in plan.peers
        ]
    for peer in plan.peers:
        razones = hard_exclusions(
            peer.dialog,
            me_id=me_id,
            whitelist_ids=whitelist_ids,
            whitelist_usernames=whitelist_usernames,
            contact_ids=contact_ids,
            protect_contacts=protect_contacts,
        )
        if razones:
            encontrados.append((peer.dialog.id, "; ".join(razones)))
    return encontrados


def _steps_for(dialog: DialogInfo, options: PlannerOptions) -> list[PlanStep]:
    steps: list[PlanStep]
    # Mensajes propios (grupo/canal/privado/bot): SIEMPRE antes de salir/block,
    # y disponibles solos (leave=False) para chats que se conservan.
    paso_propio: PlanStep | None = None
    if options.delete_own == "all":
        paso_propio = PlanStep(
            kind=StepKind.DELETE_OWN_FOR_ALL,
            detail="borrar tus mensajes para todos donde se pueda (≤48h; luego degrada a solo-ti)",
        )
    elif options.delete_own == "me":
        paso_propio = PlanStep(
            kind=StepKind.DELETE_OWN_FOR_ME,
            detail="borrar tus mensajes SOLO para ti (sin límite de edad)",
        )

    if dialog.kind is PeerKind.BOT:
        # leave=False se respeta en TODOS los tipos: "chats que conservas"
        # no puede borrar el chat de todos modos (hallazgo de robustez).
        steps = []
        if paso_propio is not None:
            steps.append(paso_propio)
        if options.leave:
            steps.append(PlanStep(kind=StepKind.DELETE_CHAT, detail="eliminar chat del bot"))
            if options.block_bots:
                steps.append(PlanStep(kind=StepKind.BLOCK, detail="bloquear bot"))
        return steps
    if dialog.kind is PeerKind.USER:
        steps = []
        if paso_propio is not None:
            steps.append(paso_propio)
        if options.leave:
            kind = (
                StepKind.DELETE_CHAT_REVOKE
                if options.revoke_private
                else StepKind.DELETE_CHAT
            )
            steps.append(PlanStep(kind=kind, detail="eliminar chat privado"))
            if options.block_private:
                steps.append(PlanStep(kind=StepKind.BLOCK, detail="bloquear usuario"))
        return steps
    if dialog.kind is PeerKind.GROUP:
        # Orden contractual: mensajes propios → historial → salir. El historial
        # solo se limpia al SALIR (leave=False promete conservar el chat;
        # vaciarlo contradiría esa promesa).
        steps = []
        if paso_propio is not None:
            steps.append(paso_propio)
        if options.leave and options.clear_history_before_leave:
            steps.append(PlanStep(kind=StepKind.CLEAR_HISTORY, detail="vaciar historial local"))
        if options.leave:
            steps.append(PlanStep(kind=StepKind.LEAVE, detail="salir del grupo"))
        return steps
    if dialog.kind is PeerKind.CHANNEL:
        # Orden contractual (igual que GROUP): mensajes propios → salir.
        # Salir SIN borrar primero destruye la única ventana para borrar los
        # mensajes propios del canal (quien abandona ya no puede borrarlos).
        steps = []
        if paso_propio is not None:
            steps.append(paso_propio)
        if options.leave:
            steps.append(PlanStep(kind=StepKind.LEAVE, detail="darse de baja del canal"))
        return steps
    return []
