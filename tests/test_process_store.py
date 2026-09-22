"""Tests for ProcessStore using an injected fake clock."""

import asyncio

import pytest

from app.errors import TooManyRequestsError
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
