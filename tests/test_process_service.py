"""Tests for ProcessService concurrency, pending coordination and errors."""

import asyncio

import pytest

from app.errors import (
    ConflictError,
    PayloadTooLargeError,
    ProcessError,
    TooManyRequestsError,
)
from app.process_service import ProcessService
from app.process_store import ProcessStore


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _BlockingEngine:
    """Engine that can be paused to exercise pending coordination."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def mask(self, text: str) -> tuple[str, int, list[str]]:
        self.calls += 1
        await self.release.wait()
        return text.replace("Иванов Иван Иванович", "__PII_PERSON_1__"), 1, ["PERSON"]


class _FailingEngine:
    async def mask(self, text: str) -> tuple[str, int, list[str]]:
        raise RuntimeError("boom")


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


def _make_service(engine, clock: _FakeClock, **kwargs) -> ProcessService:
    store = ProcessStore(
        time_func=clock,
        active_ttl=900.0,
        completed_ttl=120.0,
        max_entries=100,
        max_bytes=1_000_000,
    )
    return ProcessService(engine=engine, store=store, **kwargs)


def test_same_payload_waits_for_producer(clock: _FakeClock) -> None:
    engine = _BlockingEngine()
    service = _make_service(engine, clock)

    async def run() -> None:
        producer = asyncio.create_task(service.process("Иванов Иван Иванович", "id-1"))
        await asyncio.sleep(0.01)
        waiter = asyncio.create_task(service.process("Иванов Иван Иванович", "id-1"))
        await asyncio.sleep(0.01)
        assert engine.calls == 1  # only one masking ran

        engine.release.set()
        results = await asyncio.gather(producer, waiter)
        assert results == ["__PII_PERSON_1__", "__PII_PERSON_1__"]

    asyncio.run(run())


def test_different_payload_gets_409(clock: _FakeClock) -> None:
    engine = _BlockingEngine()
    service = _make_service(engine, clock)

    async def run() -> None:
        producer = asyncio.create_task(service.process("Иванов Иван Иванович", "id-1"))
        await asyncio.sleep(0.01)
        with pytest.raises(ConflictError):
            await service.process("Другой текст", "id-1")
        engine.release.set()
        await producer

    asyncio.run(run())


def test_waiter_timeout_returns_429_without_cancelling_producer(
    clock: _FakeClock,
) -> None:
    engine = _BlockingEngine()
    service = _make_service(engine, clock, waiter_timeout=0.05)

    async def run() -> None:
        producer = asyncio.create_task(service.process("Иванов Иван Иванович", "id-1"))
        await asyncio.sleep(0.01)
        with pytest.raises(TooManyRequestsError):
            await service.process("Иванов Иван Иванович", "id-1")

        # Producer still running, not cancelled.
        assert not producer.done()
        engine.release.set()
        assert await producer == "__PII_PERSON_1__"

    asyncio.run(run())


def test_producer_error_releases_pending_and_raises(clock: _FakeClock) -> None:
    service = _make_service(_FailingEngine(), clock)

    async def run() -> None:
        # Fail-closed: a detector error surfaces as a controlled ProcessError.
        with pytest.raises(ProcessError):
            await service.process("Иванов Иван Иванович", "id-1")
        # Capacity released, pending removed -> next call starts fresh.
        assert service.store.current_bytes == 0

    asyncio.run(run())


def test_payload_too_large_returns_413(clock: _FakeClock) -> None:
    service = _make_service(_BlockingEngine(), clock, max_payload_bytes=10)

    async def run() -> None:
        with pytest.raises(PayloadTooLargeError):
            await service.process("Иванов Иван Иванович", "id-1")

    asyncio.run(run())


def test_estimated_tokens_limit_is_separate(clock: _FakeClock) -> None:
    """Token limit must be enforced independently of the byte limit."""
    service = _make_service(
        _BlockingEngine(), clock, max_payload_bytes=1_000_000, max_estimated_tokens=4
    )

    async def run() -> None:
        # "Иванов Иван Иванович" is ~20 chars -> ~5 tokens, within byte limit
        # but over the token limit.
        with pytest.raises(PayloadTooLargeError):
            await service.process("Иванов Иван Иванович", "id-1")

    asyncio.run(run())


def test_future_has_no_unhandled_exception(clock: _FakeClock) -> None:
    service = _make_service(_FailingEngine(), clock)

    async def run() -> None:
        with pytest.raises(ProcessError):
            await service.process("Иванов Иван Иванович", "id-1")
        # Give the event loop a chance to surface any unhandled future exception.
        await asyncio.sleep(0.01)

    asyncio.run(run())


def test_ascii_and_utf8_payloads(clock: _FakeClock) -> None:
    class _EchoEngine:
        async def mask(self, text: str) -> tuple[str, int, list[str]]:
            return text, 0, []

    service = _make_service(_EchoEngine(), clock)

    async def run() -> None:
        ascii_result = await service.process("hello world", "id-ascii")
        utf8_result = await service.process("привет мир", "id-utf8")
        assert ascii_result == "hello world"
        assert utf8_result == "привет мир"

    asyncio.run(run())
