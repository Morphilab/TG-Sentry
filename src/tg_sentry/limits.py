"""Límites anti-baneo: constantes duras y política configurable.

- Los topes de sesión (50/h, 200/día) son CONSTANTES de código por diseño:
  ninguna config puede subirlos.
- writes_per_min y los caps por tipo SOLO se configuran a la baja; subirlos
  exige editar el TOML a mano Y pasar --ack-over-limit al aplicar.
"""

from __future__ import annotations

from tg_sentry.config import LimitsSection

# Topes duros de sesión — NO configurables (invariante de proyecto).
MAX_DESTRUCTIVE_PER_HOUR = 50
MAX_DESTRUCTIVE_PER_DAY = 200

# Defaults de la política configurable; subirlos exige --ack-over-limit.
DEFAULT_WRITES_PER_MIN = 4
DEFAULT_LEAVE_GROUPS_PER_HOUR = 30
DEFAULT_BLOCK_PER_HOUR = 50
DEFAULT_DELETE_MESSAGES_PER_HOUR = 100


class LimitsPolicyError(Exception):
    """Política de límites por encima del default sin reconocimiento explícito."""


def validate_policy(limits: LimitsSection, *, ack_over_limit: bool) -> None:
    """Rechaza valores por encima del default sin --ack-over-limit."""
    raised: list[str] = []
    if limits.writes_per_min > DEFAULT_WRITES_PER_MIN:
        raised.append(f"writes_per_min={limits.writes_per_min} (default {DEFAULT_WRITES_PER_MIN})")
    if limits.leave_groups_per_hour > DEFAULT_LEAVE_GROUPS_PER_HOUR:
        raised.append(
            f"leave_groups_per_hour={limits.leave_groups_per_hour}"
            f" (default {DEFAULT_LEAVE_GROUPS_PER_HOUR})"
        )
    if limits.block_per_hour > DEFAULT_BLOCK_PER_HOUR:
        raised.append(f"block_per_hour={limits.block_per_hour} (default {DEFAULT_BLOCK_PER_HOUR})")
    if limits.delete_messages_per_hour > DEFAULT_DELETE_MESSAGES_PER_HOUR:
        raised.append(
            f"delete_messages_per_hour={limits.delete_messages_per_hour}"
            f" (default {DEFAULT_DELETE_MESSAGES_PER_HOUR})"
        )
    if raised and not ack_over_limit:
        detalle = "; ".join(raised)
        msg = (
            "límites por encima del default (subir el rate aumenta el riesgo de "
            f"baneo): {detalle}. Edita el TOML conscientemente y pasa "
            "--ack-over-limit si de verdad quieres subirlos."
        )
        raise LimitsPolicyError(msg)
