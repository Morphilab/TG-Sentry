"""Runtime de Telethon: event loop propio en hilo dedicado.

CAUSA RAÍZ (hallazgo M4): Textual instala `asyncio.eager_task_factory` en el
loop del driver real. Con tareas eager, los `create_task(self._send_loop())`
de Telethon ejecutan ANTES de que `MTProtoSender.connect` ponga
`_user_connected = True`, y los loops de envío/recepción mueren al nacer:
`client.connect()` cuelga para siempre (el RPC InvokeWithLayer jamás recibe
respuesta).

Solución: Telethon corre en SU loop (hilo dedicado, sin factories ajenos) y
la TUI le entrega coroutines con `run_coroutine_threadsafe`. El CLI no usa
este módulo (su loop está limpio).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any

from tg_sentry.models import DialogInfo
from tg_sentry.planner import StepKind
from tg_sentry.tg.ops import execute_step

THREAD_NAME = "tg-sentry-telethon"

# Techo duro por llamada puentada. Generoso a propósito (refresh de cientos
# de diálogos, lotes de borrado propios): nunca debe dispararse en uso
# normal. Cierra el hueco TOCTOU residual (2ª auditoría): el hilo puede morir
# ENTRE el check de abajo y el run_coroutine_threadsafe — el futuro jamás se
# resuelve y el await se cuelga eternamente, el deadlock exacto que este
# módulo promete prevenir.
CALL_TIMEOUT_S = 900.0


class TelethonRuntime:
    """Loop dedicado para todas las operaciones de Telethon (solo TUI)."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._bombeo, name=THREAD_NAME, daemon=True
        )
        self._thread.start()

    def _bombeo(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def call(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Ejecuta la coroutine en el loop de Telethon y espera el resultado.

        Las excepciones cruzan el puente intactas (wrap_future). Si el hilo
        del runtime MURIÓ sin cerrar el loop (excepción en _bombeo), falla
        ALTO en vez de colgar para siempre en un futuro jamás resuelto
        (auditoría 2026-09).
        """
        if self._loop.is_closed() or not self._thread.is_alive():
            msg = "runtime de Telethon no disponible (cerrado o hilo muerto)"
            raise RuntimeError(msg)
        futuro = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(futuro), timeout=CALL_TIMEOUT_S
            )
        except TimeoutError:
            futuro.cancel()  # el hilo murió tras el check: no va a resolver
            msg = (
                f"la operación de Telethon no respondió en {int(CALL_TIMEOUT_S)}s "
                "(runtime atascado): reintenta o reinicia la TUI"
            )
            raise RuntimeError(msg) from None

    def shutdown(self) -> None:
        """Detiene el loop y recoge el hilo (idempotente, robusto).

        Si el hilo no para en el timeout NO se hace close() de un loop en
        ejecución (RuntimeError al salir de la TUI): se registra y se deja
        que el proceso termine (el hilo es daemon) — auditoría 2026-09.
        """
        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            return  # el loop murió entre el check y el call
        if threading.current_thread() is self._thread:
            return  # shutdown desde el propio hilo: join(self) es RuntimeError
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            logging.getLogger(__name__).warning(
                "el hilo de Telethon no terminó en 3s: no se cierra el loop "
                "(se libera al salir el proceso)"
            )
            return
        if not self._loop.is_closed():
            self._loop.close()


class BridgedGateway:
    """Gateway del ejecutor que corre cada paso en el loop de Telethon."""

    def __init__(self, runtime: TelethonRuntime, client: object) -> None:
        self._runtime = runtime
        self._client = client

    async def run_step(self, step: StepKind, dialog: DialogInfo) -> int | None:
        # PROPAGA el conteo de borrados (cooldown proporcional, M6.2):
        # antes se descartaba y el motor asumía lo conservador — auditoría 2026-09.
        conteo = await self._runtime.call(execute_step(self._client, step, dialog))
        return conteo if isinstance(conteo, int) else None
