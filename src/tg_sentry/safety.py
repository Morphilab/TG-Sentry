"""Capa de seguridad: blindados, whitelist y protecciones.

Dos niveles:
- **Exclusiones duras** (`hard_exclusions`): el peer NO entra al plan jamás.
- **Avisos blandos** (`soft_warnings`): entran al plan pero quedan marcados
  en la tabla del plan (hoy NO bloquean: el opt-in por ítem llega con la
  pantalla de revisión individual — deuda documentada).

FAIL-CLOSED: sin `me_id` no hay operación. Si no se puede probar cuál es la
propia cuenta, NADA es seleccionable ni planificable (hallazgo auditoría
2026-09: la blindadura de la propia cuenta era fail-open con me_id=None).

`me_id` se obtiene del inventario (meta `me_id`, guardada en cada refresh) o
de `client.get_me()`; NUNCA se adivina.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tg_sentry.models import DialogInfo, PeerKind, PeerRole

# Notificaciones de servicio de Telegram (aparece como usuario "Telegram").
SERVICE_PEER_IDS = frozenset({777000})

# Los ids marcados de canal son -(10**12 + id_crudo); los de grupo, -id_crudo.
_CANAL_MARCA = 10**12


def parse_whitelist(entries: list[str]) -> tuple[set[int], set[str]]:
    """Separa la whitelist de config en ids numéricos y usernames.

    Los ids se AMAN por todas sus formas (canal marcado -100…, id crudo
    positivo, grupo negado): la whitelist protege, así que una forma
    ambigua debe proteger DE MÁS, nunca de menos (auditoría 2026-09).
    """
    ids: set[int] = set()
    usernames: set[str] = set()
    for entry in entries:
        text = entry.strip().lstrip("@")
        if text.lstrip("-").isdigit():
            # "--77" pasaba lstrip("-").isdigit() y reventaba con ValueError
            # crudo (3ª auditoría); un id malformado se RECHAZA con mensaje —
            # tratarlo como username dejaría de proteger en silencio
            try:
                raw = int(text)
            except ValueError as exc:
                raise ValueError(
                    f"entrada de whitelist no válida: {entry!r} "
                    "(se espera un id numérico, con o sin '-', o un username)"
                ) from exc
            ids.add(raw)
            ids.add(-raw)
            if raw > 0:
                ids.add(-(raw + _CANAL_MARCA))  # canal marcado desde id crudo
            elif raw < -_CANAL_MARCA:
                ids.add(raw + _CANAL_MARCA)  # id crudo desde canal marcado
        elif text:
            usernames.add(text.lower())
    return ids, usernames


def hard_exclusions(
    dialog: DialogInfo,
    *,
    me_id: int | None,
    whitelist_ids: set[int],
    whitelist_usernames: set[str],
    contact_ids: set[int] | None = None,
    protect_contacts: bool = True,
) -> list[str]:
    """Razones por las que el peer queda FUERA del plan (vacío = puede entrar).

    `me_id=None` excluye TODO: sin identidad propia verificada no se puede
    garantizar ninguna blindadura (fail-closed).
    """
    if me_id is None:
        return [
            "sin identidad propia (me_id): refresca el inventario antes de operar"
        ]
    reasons: list[str] = []
    if dialog.id == me_id:
        reasons.append("tu propia cuenta / Saved Messages")
    if dialog.id in SERVICE_PEER_IDS:
        reasons.append("notificaciones de servicio de Telegram")
    if dialog.id in whitelist_ids:
        reasons.append("en tu whitelist")
    if dialog.username and dialog.username.lower() in whitelist_usernames:
        reasons.append("en tu whitelist (@)")
    if dialog.kind is PeerKind.SECRET:
        reasons.append("chat secreto: no soportado")
    if dialog.role is PeerRole.CREATOR:
        reasons.append("eres creador: transferir/borrar es operación aparte")
    if protect_contacts and contact_ids and dialog.id in contact_ids:
        reasons.append(
            "contacto tuyo (protección dura; protect_contacts=false para desactivar)"
        )
    if dialog.kind is PeerKind.UNKNOWN:
        reasons.append("tipo de peer desconocido")
    return reasons


def soft_warnings(
    dialog: DialogInfo,
    *,
    recent_private_days: int = 30,
    now: datetime | None = None,
) -> list[str]:
    """Avisos que hoy son INFORMATIVOS (no bloquean; ver docstring del módulo)."""
    warnings: list[str] = []
    reference = now or datetime.now(UTC)
    if dialog.role is PeerRole.ADMIN:
        warnings.append("eres admin: al salir pierdes el rol")
    if dialog.role is None and dialog.kind in (PeerKind.GROUP, PeerKind.CHANNEL):
        # role exige GetParticipant por peer (spike): mientras no se consulte,
        # no se puede descartar que el usuario sea creador (blindado de fábrica).
        warnings.append("rol sin verificar: no se puede descartar que seas creador")
    if (
        dialog.kind is PeerKind.USER
        and recent_private_days > 0
        and dialog.last_message_at is not None
        and (reference - dialog.last_message_at) < timedelta(days=recent_private_days)
    ):
        warnings.append(f"actividad reciente (<{recent_private_days}d): revisa antes de confirmar")
    return warnings
