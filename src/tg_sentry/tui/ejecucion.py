"""Estado de una ejecución a nivel de APP (no de pantalla).

Por qué existe (hallazgo M6.1): los workers de una Screen de Textual son
CANCELADOS al desmontar la pantalla — "Volver al dashboard" mataba la cola
en silencio aunque el binding dijera "no aborta". La ejecución vive ahora
en SentryApp (tarea asyncio propia) y la ExecuteScreen es SOLO una vista
que se adjunta/desadjunta.

Campos leídos por la vista con set_interval (sin callbacks cruzados):
- estado: iniciando | corriendo | fin | abortada | error
- log: líneas acumuladas (la vista lleva su índice leído)
- mensaje: línea de estado grande (cooldown, conexión, etc.)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from tg_sentry.executor import RunResult
from tg_sentry.planner import ActionPlan


@dataclass
class Ejecucion:
    plan: ActionPlan
    estado: str = "iniciando"
    log: list[str] = field(default_factory=list)
    mensaje: str = ""
    hechos: int = 0
    total: int = 0
    resultado: RunResult | None = None
    pasos_borrado_pendientes: int = 0
    cooldown_hasta: datetime | None = None  # mientras duerme un cooldown >=60s
    tarea: asyncio.Task[None] | None = None  # asignada al arrancar; no serializa

    @property
    def activa(self) -> bool:
        tarea = self.tarea
        return self.estado in {"iniciando", "corriendo"} and (
            tarea is None or not tarea.done()
        )

    def loguear(self, linea: str) -> None:
        # el log se pinta en la UI sin pasar por el filtro de logging:
        # redacta aquí (excepciones futuras podrían arrastrar secretos)
        from tg_sentry import redact as _redact

        self.log.append(_redact.redact(linea))
