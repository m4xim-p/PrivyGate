"""Contract tests for the /process state machine.

These tests fix the external contract from docs/evaluation-contract.md:
masking, demasking, idempotent retries, conflict handling and the
ACTIVE -> COMPLETED lifecycle. They exercise ProcessService through its
public API and assert only observable behaviour.
"""

import asyncio

import pytest

from app.errors import ConflictError, GoneError
from app.process_service import ProcessService
from app.process_store import ProcessStore


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeEngine:
    """Deterministic masking engine for contract tests."""

    def __init__(self) -> None:
        self.calls = 0

    async def mask(
        self, text: str, policy: object | None = None
    ) -> tuple[str, int, list[str]]:
        self.calls += 1
        if "Иванов" in text:
            return text.replace("Иванов Иван Иванович", "__PII_PERSON_1__"), 1, ["PERSON"]
        return text, 0, []


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def service(clock: _FakeClock) -> ProcessService:
    store = ProcessStore(
        time_func=clock,
        active_ttl=900.0,
        completed_ttl=120.0,
        max_entries=100,
        max_bytes=1_000_000,
    )
    return ProcessService(engine=_FakeEngine(), store=store)


def test_new_payload_is_masked(service: ProcessService) -> None:
    async def run() -> None:
        result = await service.process("Иванов Иван Иванович", "id-1")
        assert result == "__PII_PERSON_1__"

    asyncio.run(run())


def test_retry_original_returns_same_mask(service: ProcessService) -> None:
    async def run() -> None:
        first = await service.process("Иванов Иван Иванович", "id-1")
        second = await service.process("Иванов Иван Иванович", "id-1")
        assert first == second == "__PII_PERSON_1__"

    asyncio.run(run())


def test_demasking_returns_original(service: ProcessService) -> None:
    async def run() -> None:
        masked = await service.process("Иванов Иван Иванович", "id-1")
        original = await service.process(masked, "id-1")
        assert original == "Иванов Иван Иванович"

    asyncio.run(run())


def test_retry_original_after_completed_returns_same_mask(
    service: ProcessService,
) -> None:
    async def run() -> None:
        masked = await service.process("Иванов Иван Иванович", "id-1")
        await service.process(masked, "id-1")  # -> COMPLETED
        again = await service.process("Иванов Иван Иванович", "id-1")
        assert again == masked

    asyncio.run(run())


def test_retry_masked_after_completed_returns_original(
    service: ProcessService,
) -> None:
    async def run() -> None:
        masked = await service.process("Иванов Иван Иванович", "id-1")
        await service.process(masked, "id-1")  # -> COMPLETED
        original = await service.process(masked, "id-1")
        assert original == "Иванов Иван Иванович"

    asyncio.run(run())


def test_conflicting_payload_returns_409(service: ProcessService) -> None:
    async def run() -> None:
        await service.process("Иванов Иван Иванович", "id-1")
        with pytest.raises(ConflictError):
            await service.process("Совсем другой текст", "id-1")

    asyncio.run(run())


def test_conflict_does_not_change_session(service: ProcessService) -> None:
    """A 409 must not overwrite or corrupt the existing session."""

    async def run() -> None:
        masked = await service.process("Иванов Иван Иванович", "id-1")

        # Conflicting payload -> 409, session must stay intact.
        with pytest.raises(ConflictError):
            await service.process("Совсем другой текст", "id-1")

        # Original still returns the same mask.
        assert await service.process("Иванов Иван Иванович", "id-1") == masked
        # Masked still demasks to the original.
        assert await service.process(masked, "id-1") == "Иванов Иван Иванович"

    asyncio.run(run())


def test_original_equals_masked_is_handled(service: ProcessService) -> None:
    async def run() -> None:
        result = await service.process("просто текст", "id-1")
        assert result == "просто текст"
        assert await service.process("просто текст", "id-1") == "просто текст"

    asyncio.run(run())


def test_expired_id_returns_410(service: ProcessService, clock: _FakeClock) -> None:
    async def run() -> None:
        await service.process("Иванов Иван Иванович", "id-1")
        clock.advance(901.0)
        await service.store.get("id-1")  # triggers eviction -> tombstone

        with pytest.raises(GoneError):
            await service.process("Иванов Иван Иванович", "id-1")

    asyncio.run(run())
