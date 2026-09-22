"""Integration tests for the POST /process endpoint."""

import asyncio

import httpx

from app.main import app
from app.pii import PIIMatch, default_rule_detectors
from app.pii_engine import PIIMaskingEngine
from app.process_service import ProcessService
from app.process_store import ProcessStore


def _init_service() -> None:
    app.state.process_service = ProcessService(
        engine=PIIMaskingEngine(default_rule_detectors()),
        store=ProcessStore(
            time_func=__import__("time").monotonic,
            active_ttl=900.0,
            completed_ttl=120.0,
            max_entries=1000,
            max_bytes=1_000_000,
        ),
        waiter_timeout=5.0,
        max_payload_bytes=400_000,
    )


def test_process_masks_and_demasks() -> None:
    _init_service()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            payload = "Иванов Иван Иванович, паспорт 45 10 123456"
            masked_resp = await client.post(
                "/process",
                json={"payload": payload, "payload_id": "it-1"},
            )
            assert masked_resp.status_code == 200
            masked = masked_resp.json()["result"]
            assert "Иванов Иван Иванович" not in masked
            assert "45 10 123456" not in masked

            original_resp = await client.post(
                "/process",
                json={"payload": masked, "payload_id": "it-1"},
            )
            assert original_resp.status_code == 200
            assert original_resp.json()["result"] == payload

    asyncio.run(run())


def test_process_conflict_returns_409() -> None:
    _init_service()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            await client.post(
                "/process",
                json={"payload": "Иванов Иван Иванович", "payload_id": "it-2"},
            )
            resp = await client.post(
                "/process",
                json={"payload": "Другой текст", "payload_id": "it-2"},
            )
            assert resp.status_code == 409

    asyncio.run(run())


def test_process_missing_field_returns_422() -> None:
    _init_service()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            resp = await client.post("/process", json={"payload": "текст"})
            assert resp.status_code == 422

    asyncio.run(run())


def test_process_accepts_empty_payload_id_as_contract_string() -> None:
    """Official OpenAPI constrains payload_id by type only, without minLength."""
    _init_service()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            payload = "Иванов Иван Иванович"
            masked_resp = await client.post(
                "/process",
                json={"payload": payload, "payload_id": ""},
            )
            assert masked_resp.status_code == 200
            masked = masked_resp.json()["result"]
            assert masked != payload

            original_resp = await client.post(
                "/process",
                json={"payload": masked, "payload_id": ""},
            )
            assert original_resp.status_code == 200
            assert original_resp.json()["result"] == payload

    asyncio.run(run())


def test_process_422_does_not_leak_pii() -> None:
    _init_service()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            # payload is an object, not a string -> 422. The response must not
            # echo the offending input back (it may contain raw PII).
            resp = await client.post(
                "/process",
                json={"payload": {"secret": "Иванов Иван Иванович"}, "payload_id": "x"},
            )
            assert resp.status_code == 422
            body = resp.text
            assert "Иванов" not in body
            assert "secret" not in body

    asyncio.run(run())


def test_process_does_not_leak_pii_in_response() -> None:
    _init_service()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            payload = "Иванов Иван Иванович, +7 999 123-45-67, test@example.com"
            resp = await client.post(
                "/process",
                json={"payload": payload, "payload_id": "it-3"},
            )
            body = resp.json()["result"]
            assert "Иванов" not in body
            assert "+7 999 123-45-67" not in body
            assert "test@example.com" not in body

    asyncio.run(run())


class _FailingDetector:
    """A detector that always raises, simulating an unavailable component."""

    pii_type = "PERSON"

    def detect(self, text: str) -> list[PIIMatch]:
        raise RuntimeError("detector unavailable")


def test_process_fail_closed_on_detector_error() -> None:
    """If a detector fails, /process must return 5xx, not raw text as a mask."""

    def _init_failing() -> None:
        app.state.process_service = ProcessService(
            engine=PIIMaskingEngine([_FailingDetector()]),
            store=ProcessStore(
                time_func=__import__("time").monotonic,
                active_ttl=900.0,
                completed_ttl=120.0,
                max_entries=1000,
                max_bytes=1_000_000,
            ),
            waiter_timeout=5.0,
            max_payload_bytes=400_000,
        )

    _init_failing()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            resp = await client.post(
                "/process",
                json={"payload": "Иванов Иван Иванович", "payload_id": "fail-1"},
            )
            # Fail-closed: 5xx, and the raw PII must not be returned as a mask.
            assert resp.status_code >= 500
            assert "Иванов" not in resp.text

    asyncio.run(run())
