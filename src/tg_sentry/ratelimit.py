"""Rate limiting: token bucket con disciplina FloodWait + patrón de baneo.

Reglas (invariante de proyecto):
- FloodWait SIEMPRE manda: pausa el bucket; durante la pausa NO se acumulan
  tokens (al reanudar, bucket vacío).
- Tras un FloodWait hay un periodo de gracia de 2x la espera con tasa
  reducida (multiplicador 0.5).
- Dos FloodWait dentro de la ventana (default 300s) = patrón de baneo
  detectado → el motor debe abortar y solo reanudar manualmente.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

SleepFn = Callable[[float], Awaitable[None]]

GRACE_MULTIPLIER = 0.5
GRACE_FACTOR = 2.0  # gracia = 2x la espera del FloodWait


class TokenBucket:
    def __init__(self, rate_per_min: int, capacity: int | None = None) -> None:
        self._rate = max(1, rate_per_min)
        self._capacity = float(capacity if capacity is not None else max(1, rate_per_min))
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._pause_until: float | None = None
        self._grace_until: float | None = None

    @property
    def rate_per_min(self) -> int:
        return self._rate

    def _current_rate(self) -> float:
        """Tokens/min efectivos: reducidos durante la gracia post-FloodWait."""
        if self._grace_until is not None and time.monotonic() < self._grace_until:
            return self._rate * GRACE_MULTIPLIER
        return float(self._rate)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._current_rate() / 60)
        self._last = now

    async def acquire(self, wait: SleepFn | None = None) -> None:
        """Espera hasta haber token disponible (respeta pausas de FloodWait)."""
        sleeper = wait if wait is not None else asyncio.sleep
        while True:
            if self._pause_until is not None:
                remaining = self._pause_until - time.monotonic()
                if remaining > 0:
                    await sleeper(remaining)
                    continue
                self._pause_until = None
                self._tokens = 0.0  # sin acumulación durante la pausa
                self._last = time.monotonic()
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            deficit = (1.0 - self._tokens) / (self._current_rate() / 60)
            await sleeper(max(deficit, 0.001))

    def floodwait_pause(self, seconds: float) -> None:
        """Aplica la disciplina FloodWait: pausa, bucket vacío, gracia 2x."""
        now = time.monotonic()
        self._pause_until = now + seconds
        self._grace_until = now + seconds + GRACE_FACTOR * seconds
        self._tokens = 0.0

    def is_paused(self) -> bool:
        return self._pause_until is not None and time.monotonic() < self._pause_until

    def is_in_grace(self) -> bool:
        return self._grace_until is not None and time.monotonic() < self._grace_until


class FloodWaitMonitor:
    """Detecta el patrón de baneo: N FloodWait dentro de una ventana."""

    def __init__(self, pattern_size: int = 2) -> None:
        self._pattern_size = pattern_size
        self._events: deque[float] = deque()

    def record(self) -> None:
        self._events.append(time.monotonic())

    def pattern_detected(self, window_s: float) -> bool:
        while self._events and time.monotonic() - self._events[0] > window_s:
            self._events.popleft()
        return len(self._events) >= self._pattern_size
