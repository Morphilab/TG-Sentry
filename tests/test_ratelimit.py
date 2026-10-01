"""Tests del token bucket con disciplina FloodWait y del monitor de patrón."""

from __future__ import annotations

import asyncio

from tg_sentry.ratelimit import FloodWaitMonitor, TokenBucket


class SleepRecorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_bucket_con_tasa_alta_adquiere_inmediato() -> None:
    bucket = TokenBucket(rate_per_min=100_000)

    async def run() -> None:
        await asyncio.wait_for(bucket.acquire(), timeout=1.0)

    asyncio.run(run())


def test_floodwait_pausa_bucket_y_lo_vacia() -> None:
    bucket = TokenBucket(rate_per_min=100_000)
    recorder = SleepRecorder()
    bucket.floodwait_pause(0.05)
    assert bucket.is_paused()
    assert bucket.is_in_grace()  # gracia = 2x la espera

    async def run() -> None:
        await asyncio.wait_for(bucket.acquire(wait=recorder), timeout=2.0)

    asyncio.run(run())
    # esperó la pausa y no acumuló tokens durante ella
    assert any(s >= 0.04 for s in recorder.sleeps)
    assert not bucket.is_paused()


def test_gracia_reduce_la_tasa() -> None:
    bucket = TokenBucket(rate_per_min=100)
    assert bucket._current_rate() == 100.0
    bucket.floodwait_pause(10)
    assert bucket._current_rate() == 50.0


def test_monitor_patron_dos_floodwait_en_ventana() -> None:
    monitor = FloodWaitMonitor()
    monitor.record()
    assert not monitor.pattern_detected(window_s=300)
    monitor.record()
    assert monitor.pattern_detected(window_s=300)


def test_monitor_fuera_de_ventana_no_es_patron() -> None:
    monitor = FloodWaitMonitor()
    monitor.record()
    monitor.record()
    assert not monitor.pattern_detected(window_s=0.0)
