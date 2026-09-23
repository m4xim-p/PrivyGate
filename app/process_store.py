"""In-memory TTL store for /process sessions with pending coordination."""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from app.errors import GoneError, TooManyRequestsError


class SessionState(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"


@dataclass(slots=True)
class ProcessSession:
    original: str
    masked: str
    state: SessionState
    created_at: float
    last_accessed_at: float
    pii_count: int
    pii_types: list[str]
    completed_at: float | None = None
    session_bytes: int = 0
    expires_at: float = 0.0
    version: int = 0


@dataclass(slots=True)
class PendingResult:
    is_new: bool
    conflict: bool
    future: asyncio.Future[str]


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[str]
    reserved_bytes: int
    created_at: float
    fingerprint: str


class ProcessStore:
    """Bounded in-memory TTL store with atomic short-lock operations.

    The store holds only original/masked/timestamps/state/diagnostics. Mapping
    never lives here: it exists only transiently inside the masking engine.
    """

    # Approximate per-session overhead (dict entry, dataclass, payload_id,
    # pii_types list, etc.) used to bound real memory usage.
    SESSION_OVERHEAD_BYTES = 512

    def __init__(
        self,
        *,
        time_func: Callable[[], float],
        active_ttl: float = 900.0,
        completed_ttl: float = 120.0,
        max_entries: int = 25000,
        max_bytes: int = 536870912,
        tombstone_ttl: float = 3600.0,
        tombstone_max_entries: int = 50000,
    ) -> None:
        self._time = time_func
        self._active_ttl = active_ttl
        self._completed_ttl = completed_ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._tombstone_ttl = tombstone_ttl
        self._tombstone_max_entries = tombstone_max_entries

        self._sessions: dict[str, ProcessSession] = {}
        self._pending: dict[str, _Pending] = {}
        self._tombstones: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.current_bytes = 0
        # Min-heap of (expires_at, version, payload_id) for lazy eviction.
        self._expiry_heap: list[tuple[float, int, str]] = []
        self._version_counter = 0

    async def get_or_create_pending(
        self, payload_id: str, payload_bytes: int, fingerprint: str
    ) -> PendingResult:
        async with self._lock:
            existing = self._pending.get(payload_id)
            if existing is not None:
                conflict = existing.fingerprint != fingerprint
                return PendingResult(
                    is_new=False, conflict=conflict, future=existing.future
                )

            if payload_id in self._sessions:
                session = self._sessions[payload_id]
                if self._is_expired_locked(session):
                    self._evict_session_locked(payload_id, session)
                    raise GoneError("payload_id expired")
                return PendingResult(
                    is_new=False,
                    conflict=False,
                    future=self._completed_future(session),
                )

            if self._is_tombstoned_locked(payload_id):
                raise GoneError("payload_id expired")

            # Evict already-expired entries before checking capacity.
            self._evict_expired_locked()

            if len(self._pending) + len(self._sessions) >= self.max_entries:
                raise TooManyRequestsError("store entry limit reached", retry_after=1.0)
            if self.current_bytes + payload_bytes > self.max_bytes:
                raise TooManyRequestsError("store byte limit reached", retry_after=1.0)

            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._pending[payload_id] = _Pending(
                future=future,
                reserved_bytes=payload_bytes,
                created_at=self._time(),
                fingerprint=fingerprint,
            )
            self.current_bytes += payload_bytes
            return PendingResult(is_new=True, conflict=False, future=future)

    async def publish_active(
        self,
        payload_id: str,
        original: str,
        masked: str,
        pii_count: int,
        pii_types: list[str],
    ) -> None:
        async with self._lock:
            pending = self._pending.pop(payload_id, None)
            now = self._time()
            session_bytes = (
                len(original.encode("utf-8"))
                + len(masked.encode("utf-8"))
                + len(payload_id.encode("utf-8"))
                + self.SESSION_OVERHEAD_BYTES
            )
            self._version_counter += 1
            session = ProcessSession(
                original=original,
                masked=masked,
                state=SessionState.ACTIVE,
                created_at=now,
                last_accessed_at=now,
                pii_count=pii_count,
                pii_types=pii_types,
                session_bytes=session_bytes,
                expires_at=now + self._active_ttl,
                version=self._version_counter,
            )
            self._sessions[payload_id] = session
            heapq.heappush(
                self._expiry_heap, (session.expires_at, session.version, payload_id)
            )
            if pending is not None:
                self.current_bytes -= pending.reserved_bytes
            self.current_bytes += session_bytes

    async def complete(self, payload_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(payload_id)
            if session is not None and session.state is SessionState.ACTIVE:
                session.state = SessionState.COMPLETED
                session.completed_at = self._time()
                session.last_accessed_at = self._time()
                # Update expiry to COMPLETED TTL from completion time and bump
                # version so stale heap entries are ignored (lazy deletion).
                self._version_counter += 1
                session.version = self._version_counter
                session.expires_at = session.completed_at + self._completed_ttl
                heapq.heappush(
                    self._expiry_heap,
                    (session.expires_at, session.version, payload_id),
                )

    async def get(self, payload_id: str) -> ProcessSession | None:
        async with self._lock:
            session = self._sessions.get(payload_id)
            if session is None:
                return None
            if self._is_expired_locked(session):
                self._evict_session_locked(payload_id, session)
                return None
            session.last_accessed_at = self._time()
            return session

    async def release_pending(self, payload_id: str, future: asyncio.Future[str]) -> None:
        async with self._lock:
            pending = self._pending.get(payload_id)
            if pending is not None and pending.future is future:
                self._pending.pop(payload_id, None)
                self.current_bytes -= pending.reserved_bytes

    async def is_tombstoned(self, payload_id: str) -> bool:
        async with self._lock:
            return self._is_tombstoned_locked(payload_id)

    async def expire_all(self) -> None:
        async with self._lock:
            self._sessions.clear()
            self._pending.clear()
            self._tombstones.clear()
            self._expiry_heap.clear()
            self.current_bytes = 0

    def _is_tombstoned_locked(self, payload_id: str) -> bool:
        expiry = self._tombstones.get(payload_id)
        if expiry is None:
            return False
        if expiry <= self._time():
            self._tombstones.pop(payload_id, None)
            return False
        return True

    def _is_expired_locked(self, session: ProcessSession) -> bool:
        """O(1) TTL check for a single session."""
        return session.expires_at <= self._time()

    def _evict_session_locked(self, payload_id: str, session: ProcessSession) -> None:
        """Move an expired session to a tombstone and free its bytes."""
        now = self._time()
        self._sessions.pop(payload_id, None)
        self.current_bytes -= session.session_bytes
        self._tombstones[payload_id] = now + self._tombstone_ttl
        self._trim_tombstones_locked()

    def _evict_expired_locked(self, limit: int | None = None) -> int:
        """Pop expired entries from the min-heap, evicting their sessions.

        Returns the number of sessions evicted. Stale heap entries (whose
        version no longer matches the session) are skipped. ``limit`` bounds
        the work per call for background cleanup.
        """
        now = self._time()
        evicted = 0
        while self._expiry_heap:
            if limit is not None and evicted >= limit:
                break
            expires_at, version, payload_id = self._expiry_heap[0]
            if expires_at > now:
                break
            heapq.heappop(self._expiry_heap)
            session = self._sessions.get(payload_id)
            if session is None or session.version != version:
                # Stale entry (session completed/expired already) — skip.
                continue
            self._evict_session_locked(payload_id, session)
            evicted += 1

        # Also expire pending entries that outlived the ACTIVE TTL.
        expired_pending: list[str] = []
        for payload_id, pending in self._pending.items():
            if pending.created_at + self._active_ttl <= now:
                expired_pending.append(payload_id)
        for payload_id in expired_pending:
            pending = self._pending.pop(payload_id)
            self.current_bytes -= pending.reserved_bytes
            evicted += 1

        return evicted

    async def evict_expired(self, limit: int | None = None) -> int:
        """Public entry point for background eviction (bounded work per call)."""
        async with self._lock:
            return self._evict_expired_locked(limit=limit)

    def _trim_tombstones_locked(self) -> None:
        if len(self._tombstones) <= self._tombstone_max_entries:
            return
        now = self._time()
        for payload_id in list(self._tombstones):
            if len(self._tombstones) <= self._tombstone_max_entries:
                break
            if self._tombstones[payload_id] <= now:
                self._tombstones.pop(payload_id, None)

    def _completed_future(self, session: ProcessSession) -> asyncio.Future[str]:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        future.set_result(session.masked)
        return future
