"""Tests del runtime de Telethon (loop propio en hilo). Sin red.

Regresión de la causa raíz M4: Textual instala `asyncio.eager_task_factory`,
con el cual `create_task` ejecuta la coroutine AL INSTANTE — el patrón de
Telethon (crear tareas y activar el flag `_user_connected` DESPUÉS) muere al
nacer en ese loop. El runtime ejecuta en un loop limpio y por tanto es inmune.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest

from tg_sentry.tg.runtime import THREAD_NAME, TelethonRuntime


async def _patron_telethon() -> bool:
    """Réplica del patrón de MTProtoSender: create_task y flag después."""
    conectado = False

    async def worker() -> bool:
        return conectado  # eager: False (aún); normal: el valor al ejecutar

    tarea = asyncio.get_running_loop().create_task(worker())
    conectado = True  # telethon pone _user_connected aquí, tras create_task
    return await tarea


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="asyncio.eager_task_factory no existe antes de 3.12",
)
def test_el_patron_telethon_muere_con_eager_factory() -> None:
    # sanity del diagnóstico: en un loop eager, el patrón de telethon falla
    async def main() -> bool:
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
        return await _patron_telethon()

    assert asyncio.run(main()) is False


def test_el_patron_telethon_vive_en_el_runtime() -> None:
    runtime = TelethonRuntime()
    try:
        resultado = asyncio.run(runtime.call(_patron_telethon()))
        assert resultado is True
    finally:
        runtime.shutdown()


def test_runtime_ejecuta_en_hilo_dedicado() -> None:
    runtime = TelethonRuntime()
    try:

        async def nombre_hilo() -> str:
            return threading.current_thread().name

        hilo = asyncio.run(runtime.call(nombre_hilo()))
        assert hilo == THREAD_NAME
        assert threading.current_thread().name != THREAD_NAME
    finally:
        runtime.shutdown()


def test_runtime_lanza_excepciones_a_traves_del_puente() -> None:
    runtime = TelethonRuntime()
    try:

        async def falla() -> None:
            msg = "boom"
            raise ValueError(msg)

        async def main() -> object:
            try:
                await runtime.call(falla())
            except ValueError as exc:
                return exc
            return None

        error = asyncio.run(main())
        assert isinstance(error, ValueError) and str(error) == "boom"
    finally:
        runtime.shutdown()


def test_runtime_shutdown_idempotente() -> None:
    runtime = TelethonRuntime()
    runtime.shutdown()
    runtime.shutdown()  # no explota


# --- Regresiones auditoría 2026-09 (rama api) -------------------------------


def test_runtime_call_con_hilo_muerto_falla_alto() -> None:
    """Si el hilo del runtime murió sin cerrar el loop, call() NO puede
    quedarse colgada en un futuro jamás resuelto: RuntimeError inmediato."""
    import asyncio

    rt = TelethonRuntime()
    try:
        # el hilo para (run_forever termina) pero el loop queda ABIERTO:
        # el estado exacto de un _bombeo que murió sin close()
        rt._loop.call_soon_threadsafe(rt._loop.stop)
        rt._thread.join(timeout=3)
        assert not rt._thread.is_alive()
        assert not rt._loop.is_closed()
        coro = asyncio.sleep(0)
        try:
            asyncio.run(rt.call(coro))
            raise AssertionError("debía lanzar RuntimeError")
        except RuntimeError as exc:
            assert "no disponible" in str(exc)
        finally:
            # la coroutine nunca se programó (loop muerto): close() evita el
            # RuntimeWarning de GC "never awaited" (4ª auditoría)
            coro.close()
    finally:
        rt.shutdown()


def test_runtime_shutdown_no_cierra_loop_atascado() -> None:
    """close() sobre un loop EN EJECUCIÓN lanzaba RuntimeError al salir de la
    TUI; ahora se registra y el hilo (daemon) se recoge al salir el proceso."""
    import time

    rt = TelethonRuntime()
    try:
        # callback síncrono largo: mantiene el hilo ocupado MÁS que el
        # join(3) del shutdown → el stop no se procesa a tiempo
        rt._loop.call_soon_threadsafe(lambda: time.sleep(4))
        rt.shutdown()  # no debe lanzar aunque el hilo no termine en 3s
        assert rt._thread.is_alive()  # seguía atascado: no se cerró el loop
        rt._thread.join(timeout=10)  # tras el atasco, el stop lo termina
        assert not rt._thread.is_alive()
    finally:
        rt.shutdown()


def test_call_con_hilo_muerto_tras_el_check_no_cuelga(monkeypatch) -> None:
    """2ª auditoría (TOCTOU): el hilo puede morir ENTRE el check y el
    run_coroutine_threadsafe — el futuro jamás resuelve. Con el techo de
    llamada, call() falla ALTO en vez de colgar eternamente."""
    import asyncio as _asyncio

    from tg_sentry.tg import runtime as rt

    async def escena() -> None:
        runtime = rt.TelethonRuntime()
        monkeypatch.setattr(rt, "CALL_TIMEOUT_S", 0.05)

        async def nunca() -> None:
            await _asyncio.Event().wait()

        # el hilo está vivo pero la coroutine jamás termina → techo
        try:
            with pytest.raises(RuntimeError, match="no respondió"):
                await runtime.call(nunca())
        finally:
            runtime.shutdown()

    _asyncio.run(escena())


def test_restante_ttl_devuelve_cero_si_vencio() -> None:
    from datetime import timedelta

    from tests.test_inventory import _to_infos
    from tg_sentry.planner import PlannerOptions, build_plan

    plan = build_plan(_to_infos()[:1], PlannerOptions(ttl_min=5), me_id=9)
    assert plan.restante_ttl() > timedelta(0)
    pasado = plan.restante_ttl(plan.expires_at + timedelta(minutes=1))
    assert pasado == timedelta(0)
