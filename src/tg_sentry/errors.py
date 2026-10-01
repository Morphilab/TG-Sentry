"""Excepciones neutras compartidas entre capas.

El ejecutor NO importa Telethon; la capa `tg/` traduce las excepciones de
Telethon a esta taxonomía. Así el motor es testeable sin red y el mapeo de
estados vive en un único sitio.
"""

from __future__ import annotations

from typing import Literal

GoneReason = Literal[
    "not_participant",  # ya no eres miembro (lo fuiste al planificar) → skipped_ok
    "channel_private",  # el peer se volvió privado/inaccesible → skipped_external
    "peer_invalid",  # el peer nunca fue accesible así → skipped_unreachable
    "user_deactivated",  # la otra parte borró su cuenta → skipped_external
]


class StepError(Exception):
    """Fallo ejecutando un paso del plan."""


class FloodWait(StepError):
    """Telegram exige esperar N segundos antes de reintentar."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        super().__init__(f"FloodWait: {seconds}s")


class PeerGone(StepError):
    """El peer ya no está accesible de la forma esperada."""

    def __init__(self, reason: GoneReason) -> None:
        self.reason = reason
        super().__init__(f"peer gone: {reason}")


class AdminRequired(StepError):
    """La operación exige permisos de administrador que no tienes."""


class TransientNetwork(StepError):
    """Fallo de conexión transitorio: reintenta con backoff."""


class RpcFailure(StepError):
    """Error RPC genérico (no mapeado): reintenta y luego falla el ítem."""


class UnsupportedStep(StepError):
    """Paso sin primitiva segura verificada (p. ej. clear_history pre-piloto)."""


class SessionInvalid(StepError):
    """La sesión fue revocada/invalidada en el servidor: TERMINAL, no se
    reintenta (logout remoto, auth key duplicada). La cola aborta y el
    camino es re-hacer login."""


class PlanExpiredError(Exception):
    """El plan superó su TTL: hay que regenerarlo y re-confirmar."""
