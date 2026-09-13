"""In-memory asynchronous round-robin backend selection."""

import asyncio
from collections.abc import Sequence


class RoundRobinRouter:
    def __init__(self, backends: Sequence[str]) -> None:
        if not backends:
            raise ValueError("At least one backend is required")
        self._backends = tuple(url.rstrip("/") for url in backends)
        self._next_index = 0
        self._lock = asyncio.Lock()

    async def next_backend(self) -> str:
        async with self._lock:
            backend = self._backends[self._next_index]
            self._next_index = (self._next_index + 1) % len(self._backends)
            return backend
