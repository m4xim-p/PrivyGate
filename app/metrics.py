"""Lightweight runtime metrics for the gateway.

Exposes counters and store/event-loop gauges in Prometheus text format via
``GET /metrics``. Counters are cheap atomic increments (no heavy computation in
the hot path). Store sizes are read from the in-memory store on demand. Event
loop delay is sampled by a background task, not in the request path.

This module never logs raw PII and never touches the masking pipeline.
"""

from __future__ import annotations

import asyncio
import time

from app.process_store import ProcessStore


class ProcessMetrics:
    """Atomic counters for /process lifecycle events.

    All increments are O(1) and lock-free (single-threaded event loop), so they
    add negligible overhead to the hot path.
    """

    def __init__(self) -> None:
        self.mask_count = 0
        self.demask_count = 0
        self.retry_count = 0
        self.rate_limited_count = 0
        self.conflict_count = 0
        self.error_count = 0
        self.new_id_count = 0
        self.eviction_count = 0

    def inc_mask(self) -> None:
        self.mask_count += 1

    def inc_demask(self) -> None:
        self.demask_count += 1

    def inc_retry(self) -> None:
        self.retry_count += 1

    def inc_rate_limited(self) -> None:
        self.rate_limited_count += 1

    def inc_conflict(self) -> None:
        self.conflict_count += 1

    def inc_error(self) -> None:
        self.error_count += 1

    def inc_new_id(self) -> None:
        self.new_id_count += 1

    def add_evictions(self, count: int) -> None:
        self.eviction_count += count


class EventLoopDelaySampler:
    """Samples event loop responsiveness in a background task.

    Measures how long a scheduled callback is delayed relative to its target
    time. A growing delay indicates the event loop is blocked (e.g. by CPU-bound
    work). Runs as a separate asyncio task, not in the request path.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self._interval = interval
        self._delay_ms = 0.0
        self._task: asyncio.Task[None] | None = None

    async def _sample(self) -> None:
        while True:
            target = time.monotonic() + self._interval
            await asyncio.sleep(self._interval)
            actual = time.monotonic()
            self._delay_ms = max(0.0, (actual - target) * 1000.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._sample())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def delay_ms(self) -> float:
        return self._delay_ms


class StoreEvictionTask:
    """Background task that evicts expired store entries in bounded batches.

    Runs periodically and calls ``store.evict_expired(limit)`` with a small
    per-tick limit so the event loop is never blocked for long. This replaces
    the previous O(n) full-scan eviction on every request (ADR-0006).
    """

    def __init__(
        self,
        store: ProcessStore,
        metrics: ProcessMetrics,
        *,
        interval: float = 1.0,
        batch_limit: int = 1000,
    ) -> None:
        self._store = store
        self._metrics = metrics
        self._interval = interval
        self._batch_limit = batch_limit
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            evicted = await self._store.evict_expired(limit=self._batch_limit)
            if evicted:
                self._metrics.add_evictions(evicted)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def render_prometheus(
    store: ProcessStore,
    metrics: ProcessMetrics,
    event_loop_delay_ms: float,
    *,
    started_at: float,
) -> str:
    """Render runtime metrics in Prometheus text format (metric value per line)."""
    now = time.monotonic()
    uptime_seconds = now - started_at

    lines = [
        "# TYPE privygate_uptime_seconds gauge",
        f"privygate_uptime_seconds {uptime_seconds:.3f}",
        "# TYPE privygate_process_mask_total counter",
        f"privygate_process_mask_total {metrics.mask_count}",
        "# TYPE privygate_process_demask_total counter",
        f"privygate_process_demask_total {metrics.demask_count}",
        "# TYPE privygate_process_retry_total counter",
        f"privygate_process_retry_total {metrics.retry_count}",
        "# TYPE privygate_process_rate_limited_total counter",
        f"privygate_process_rate_limited_total {metrics.rate_limited_count}",
        "# TYPE privygate_process_conflict_total counter",
        f"privygate_process_conflict_total {metrics.conflict_count}",
        "# TYPE privygate_process_error_total counter",
        f"privygate_process_error_total {metrics.error_count}",
        "# TYPE privygate_process_new_id_total counter",
        f"privygate_process_new_id_total {metrics.new_id_count}",
        "# TYPE privygate_store_evictions_total counter",
        f"privygate_store_evictions_total {metrics.eviction_count}",
        "# TYPE privygate_store_sessions gauge",
        f"privygate_store_sessions {len(store._sessions)}",
        "# TYPE privygate_store_pending gauge",
        f"privygate_store_pending {len(store._pending)}",
        "# TYPE privygate_store_tombstones gauge",
        f"privygate_store_tombstones {len(store._tombstones)}",
        "# TYPE privygate_store_bytes gauge",
        f"privygate_store_bytes {store.current_bytes}",
        "# TYPE privygate_event_loop_delay_ms gauge",
        f"privygate_event_loop_delay_ms {event_loop_delay_ms:.3f}",
    ]
    return "\n".join(lines) + "\n"
