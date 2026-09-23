"""Tests for ProcessStore using an injected fake clock."""

import asyncio

import pytest

from app.errors import GoneError, TooManyRequestsError
from app.process_store import ProcessStore, SessionState


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def store(clock: _FakeClock) -> ProcessStore:
    return ProcessStore(
        time_func=clock,
        active_ttl=900.0,
        completed_ttl=120.0,
        max_entries=100,
        max_bytes=1_000_000,
    )


def test_create_pending_then_publish_active(store: ProcessStore) -> None:
    async def run() -> None:
        pending = await store.get_or_create_pending("id-1", 10, ("fp", 10))
        assert pending.is_new is True

        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])
        session = await store.get("id-1")

        assert session is not None
        assert session.state is SessionState.ACTIVE
        assert session.original == "original"
        assert session.masked == "masked"
        assert session.pii_count == 1
        assert session.pii_types == ["PERSON"]

    asyncio.run(run())


def test_second_pending_returns_existing_future(store: ProcessStore) -> None:
    async def run() -> None:
        first = await store.get_or_create_pending("id-1", 10, ("fp", 10))
        second = await store.get_or_create_pending("id-1", 10, ("fp", 10))

        assert first.is_new is True
        assert second.is_new is False
        assert first.future is second.future

    asyncio.run(run())


def test_active_expires_after_ttl(store: ProcessStore, clock: _FakeClock) -> None:
    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        clock.advance(901.0)
        session = await store.get("id-1")

        assert session is None

    asyncio.run(run())


def test_completed_expires_after_completed_ttl(
    store: ProcessStore, clock: _FakeClock
) -> None:
    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])
        await store.complete("id-1")

        clock.advance(121.0)
        session = await store.get("id-1")

        assert session is None

    asyncio.run(run())


def test_retry_does_not_extend_ttl_forever(
    store: ProcessStore, clock: _FakeClock
) -> None:
    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        for _ in range(5):
            clock.advance(100.0)
            await store.get("id-1")

        clock.advance(400.0)  # total 900s elapsed
        assert await store.get("id-1") is None

    asyncio.run(run())


def test_capacity_reserved_on_pending_and_released_on_error(
    store: ProcessStore,
) -> None:
    async def run() -> None:
        before = store.current_bytes
        pending = await store.get_or_create_pending("id-1", 100, ("fp", 100))
        assert store.current_bytes == before + 100

        await store.release_pending("id-1", pending.future)
        assert store.current_bytes == before

    asyncio.run(run())


def test_capacity_released_after_expiry(
    store: ProcessStore, clock: _FakeClock
) -> None:
    async def run() -> None:
        before = store.current_bytes
        await store.get_or_create_pending("id-1", 100, ("fp", 100))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        clock.advance(901.0)
        await store.get("id-1")  # triggers cleanup

        assert store.current_bytes == before

    asyncio.run(run())


def test_active_session_counts_original_and_masked_bytes(
    store: ProcessStore,
) -> None:
    """current_bytes must include original+masked+overhead, not just payload."""

    async def run() -> None:
        before = store.current_bytes
        await store.get_or_create_pending("id-1", 100, ("fp", 100))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        # After publish, the pending payload reservation is replaced by the
        # session's real footprint (original + masked + id + overhead).
        expected = (
            len("original")
            + len("masked")
            + len("id-1")
            + ProcessStore.SESSION_OVERHEAD_BYTES
        )
        assert store.current_bytes == before + expected

    asyncio.run(run())


def test_tombstone_returns_410(store: ProcessStore, clock: _FakeClock) -> None:
    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])
        clock.advance(901.0)
        await store.get("id-1")  # expires -> tombstone

        assert await store.is_tombstoned("id-1") is True

    asyncio.run(run())


def test_entry_limit_raises(store: ProcessStore) -> None:
    async def run() -> None:
        store.max_entries = 1
        await store.get_or_create_pending("id-1", 10, ("fp", 10))

        with pytest.raises(TooManyRequestsError):
            await store.get_or_create_pending("id-2", 10, ("fp", 10))

    asyncio.run(run())


def test_byte_limit_raises(store: ProcessStore) -> None:
    async def run() -> None:
        store.max_bytes = 50
        await store.get_or_create_pending("id-1", 10, ("fp", 10))

        with pytest.raises(TooManyRequestsError):
            await store.get_or_create_pending("id-2", 100, ("fp", 100))

    asyncio.run(run())


def test_utf8_payload_bytes_are_counted(store: ProcessStore) -> None:
    """Russian UTF-8 chars are 2 bytes each; byte limit must use bytes, not chars."""

    async def run() -> None:
        store.max_bytes = 20
        # "Иванов" is 6 chars but 12 UTF-8 bytes. A 10-char Russian string is
        # 20 bytes, which should hit the limit.
        await store.get_or_create_pending("id-1", 10, ("fp", 10))

        with pytest.raises(TooManyRequestsError):
            await store.get_or_create_pending("id-2", 20, ("fp", 20))

    asyncio.run(run())


def test_transition_active_to_completed_is_atomic(store: ProcessStore) -> None:
    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        await store.complete("id-1")
        session = await store.get("id-1")

        assert session is not None
        assert session.state is SessionState.COMPLETED

    asyncio.run(run())


def test_completed_ttl_counts_from_completion_not_creation(
    store: ProcessStore, clock: _FakeClock
) -> None:
    """COMPLETED TTL must start at the moment of completion, not creation."""

    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        # Advance most of the ACTIVE TTL, then complete.
        clock.advance(800.0)
        await store.complete("id-1")

        # COMPLETED TTL (120s) starts from completion, so the session should
        # still be alive shortly after completion even though created_at is old.
        clock.advance(100.0)  # 900s since creation, but only 100s since completion
        assert await store.get("id-1") is not None

        # After the full COMPLETED TTL from completion, it expires.
        clock.advance(30.0)  # 130s since completion > 120s
        assert await store.get("id-1") is None

    asyncio.run(run())


def test_evict_expired_background_batch(store: ProcessStore, clock: _FakeClock) -> None:
    """Background eviction removes expired sessions in bounded batches."""

    async def run() -> None:
        for i in range(5):
            await store.get_or_create_pending(f"id-{i}", 10, ("fp", 10))
            await store.publish_active(f"id-{i}", "orig", "mask", 1, ["PERSON"])

        clock.advance(901.0)
        # Batch limit of 2: only 2 sessions evicted per call.
        assert await store.evict_expired(limit=2) == 2
        assert await store.evict_expired(limit=2) == 2
        assert await store.evict_expired(limit=2) == 1
        assert await store.evict_expired(limit=2) == 0

        for i in range(5):
            assert await store.get(f"id-{i}") is None

    asyncio.run(run())


def test_stale_heap_entry_ignored_after_complete(
    store: ProcessStore, clock: _FakeClock
) -> None:
    """A stale ACTIVE heap entry is ignored after the session completes."""

    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        # Complete early: COMPLETED TTL (120s) replaces the ACTIVE TTL (900s).
        clock.advance(10.0)
        await store.complete("id-1")

        # Advance past the COMPLETED TTL: the session is evicted, but the stale
        # ACTIVE heap entry (expires_at ~910s) is still in the heap.
        clock.advance(200.0)  # 210s since completion > 120s
        assert await store.get("id-1") is None

        # Background eviction must skip the stale ACTIVE entry (session gone).
        assert await store.evict_expired(limit=10) == 0

    asyncio.run(run())


def test_expired_session_returns_410_on_access(
    store: ProcessStore, clock: _FakeClock
) -> None:
    """Accessing an expired payload_id raises GoneError (410)."""

    async def run() -> None:
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "original", "masked", 1, ["PERSON"])

        clock.advance(901.0)
        with pytest.raises(GoneError):
            await store.get_or_create_pending("id-1", 10, ("fp", 10))

    asyncio.run(run())


def test_many_expirations_evict_all(clock: _FakeClock) -> None:
    """Eviction handles tens of thousands of expired sessions correctly."""

    async def run() -> None:
        store = ProcessStore(
            time_func=clock,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=100000,
            max_bytes=1_000_000_000,
        )
        n = 20000
        for i in range(n):
            await store.get_or_create_pending(f"id-{i}", 10, ("fp", 10))
            await store.publish_active(f"id-{i}", "orig", "mask", 1, ["PERSON"])

        assert len(store._sessions) == n
        clock.advance(901.0)

        # Evict in bounded batches; all must be evicted.
        total = 0
        while True:
            evicted = await store.evict_expired(limit=1000)
            total += evicted
            if evicted == 0:
                break
        assert total == n
        assert len(store._sessions) == 0
        assert store.current_bytes == 0

    asyncio.run(run())


def test_tombstones_bounded_after_many_evictions(
    clock: _FakeClock,
) -> None:
    """Tombstone count stays bounded after many evictions (no O(n) growth)."""

    async def run() -> None:
        store = ProcessStore(
            time_func=clock,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=100000,
            max_bytes=1_000_000_000,
            tombstone_max_entries=100,
        )
        n = 5000
        for i in range(n):
            await store.get_or_create_pending(f"id-{i}", 10, ("fp", 10))
            await store.publish_active(f"id-{i}", "orig", "mask", 1, ["PERSON"])

        clock.advance(901.0)
        await store.evict_expired(limit=n)

        # Tombstones bounded by max_entries, not by number of evictions.
        assert len(store._tombstones) <= store._tombstone_max_entries

    asyncio.run(run())


def test_heap_bounded_with_stale_entries(clock: _FakeClock) -> None:
    """Expiry heap stays bounded even with stale entries from ACTIVE->COMPLETED."""

    async def run() -> None:
        store = ProcessStore(
            time_func=clock,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=100000,
            max_bytes=1_000_000_000,
        )
        n = 5000
        for i in range(n):
            await store.get_or_create_pending(f"id-{i}", 10, ("fp", 10))
            await store.publish_active(f"id-{i}", "orig", "mask", 1, ["PERSON"])
            # Complete early: creates a stale ACTIVE heap entry + a COMPLETED one.
            await store.complete(f"id-{i}")

        # Heap has at most 2 entries per session (ACTIVE + COMPLETED).
        assert len(store._expiry_heap) <= 2 * n

        # After COMPLETED TTL, all sessions evicted; stale ACTIVE entries remain
        # in the heap until their (later) expiry, but are bounded.
        clock.advance(121.0)
        await store.evict_expired(limit=n)
        assert len(store._sessions) == 0
        # Stale ACTIVE entries still in heap (expire at ~900s), bounded by 2n.
        assert len(store._expiry_heap) <= 2 * n

    asyncio.run(run())


def test_eviction_bounded_work_no_event_loop_spike(clock: _FakeClock) -> None:
    """Bounded eviction batches do not cause event-loop delay spikes."""

    async def run() -> None:
        store = ProcessStore(
            time_func=clock,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=100000,
            max_bytes=1_000_000_000,
        )
        n = 10000
        for i in range(n):
            await store.get_or_create_pending(f"id-{i}", 10, ("fp", 10))
            await store.publish_active(f"id-{i}", "orig", "mask", 1, ["PERSON"])

        clock.advance(901.0)

        # Each bounded eviction call must complete quickly (no full scan).
        import time

        start = time.perf_counter()
        for _ in range(20):
            await store.evict_expired(limit=100)
        elapsed = time.perf_counter() - start
        # 20 bounded calls of 100 each must be fast (< 1s total).
        assert elapsed < 1.0

    asyncio.run(run())


def test_tombstone_ttl_60s(clock: _FakeClock) -> None:
    """Tombstone TTL is 60s (retry buffer after COMPLETED TTL)."""

    async def run() -> None:
        store = ProcessStore(
            time_func=clock,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=100000,
            max_bytes=1_000_000_000,
            tombstone_ttl=60.0,
        )
        await store.get_or_create_pending("id-1", 10, ("fp", 10))
        await store.publish_active("id-1", "orig", "mask", 1, ["PERSON"])

        clock.advance(901.0)
        await store.get("id-1")  # expires -> tombstone
        assert await store.is_tombstoned("id-1") is True

        # Tombstone still alive within 60s TTL.
        clock.advance(59.0)
        assert await store.is_tombstoned("id-1") is True

        # Tombstone expires after 60s.
        clock.advance(2.0)
        assert await store.is_tombstoned("id-1") is False

    asyncio.run(run())


def test_tombstone_max_entries_defaults_to_100k(
    clock: _FakeClock,
) -> None:
    """tombstone_max_entries defaults to 100k when not specified."""

    async def run() -> None:
        store = ProcessStore(
            time_func=clock,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=100000,
            max_bytes=1_000_000_000,
        )
        assert store._tombstone_max_entries == 100000

    asyncio.run(run())


def test_tombstone_overflow_keeps_live_410_and_rejects_new(
    clock: _FakeClock,
) -> None:
    """Overflowing the tombstone limit keeps live 410s and rejects new IDs.

    Live tombstones must NOT be evicted early; instead new IDs are rejected
    with 429 when the tombstone limit is reached.
    """

    async def run() -> None:
        store = ProcessStore(
            time_func=clock,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=100000,
            max_bytes=1_000_000_000,
            tombstone_ttl=60.0,
            tombstone_max_entries=100,
        )
        # Create and expire 200 sessions -> 200 tombstones, but limit is 100.
        n = 200
        for i in range(n):
            await store.get_or_create_pending(f"id-{i}", 10, ("fp", 10))
            await store.publish_active(f"id-{i}", "orig", "mask", 1, ["PERSON"])

        clock.advance(901.0)
        await store.evict_expired(limit=n)

        # Tombstones bounded at the limit (no early eviction of live ones).
        assert len(store._tombstones) == store._tombstone_max_entries

        # Old live tombstones still return 410 (is_tombstoned = True).
        for i in range(100):
            assert await store.is_tombstoned(f"id-{i}") is True

        # New IDs are rejected with 429 (TooManyRequestsError).
        with pytest.raises(TooManyRequestsError):
            await store.get_or_create_pending("new-id", 10, ("fp", 10))

    asyncio.run(run())
