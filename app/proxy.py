"""Asynchronous streaming transport between the gateway and an LLM backend."""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.pii import StreamingDemasker


logger = logging.getLogger("privygate.proxy")


@dataclass(slots=True)
class UpstreamStream:
    response: httpx.Response
    body: AsyncIterator[str]


async def open_upstream_stream(
    *,
    client: httpx.AsyncClient,
    backend: str,
    payload: dict[str, object],
    mapping: dict[str, str],
    request_id: str,
    pii_types: list[str],
) -> UpstreamStream:
    started_at = time.perf_counter()
    request = client.build_request(
        "POST",
        f"{backend}/v1/chat/completions",
        json=payload,
        headers={"X-Request-ID": request_id},
    )
    response = await client.send(request, stream=True)

    if response.is_error:
        status_code = response.status_code
        await response.aclose()
        logger.warning(
            "upstream_rejected request_id=%s backend=%s status=%d pii_count=%d pii_types=%s",
            request_id,
            backend,
            status_code,
            len(mapping),
            ",".join(pii_types) or "none",
        )
        raise httpx.HTTPStatusError(
            "Upstream returned an error",
            request=request,
            response=response,
        )

    async def iter_demasked() -> AsyncIterator[str]:
        demasker = StreamingDemasker(mapping)
        status = "completed"
        try:
            async for chunk in response.aiter_text():
                restored = demasker.feed(chunk)
                if restored:
                    yield restored
            tail = demasker.flush()
            if tail:
                yield tail
        except Exception:
            status = "stream_error"
            raise
        finally:
            await response.aclose()
            latency_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "request_finished request_id=%s backend=%s status=%s latency_ms=%.1f "
                "pii_count=%d pii_types=%s",
                request_id,
                backend,
                status,
                latency_ms,
                len(mapping),
                ",".join(pii_types) or "none",
            )

    return UpstreamStream(response=response, body=iter_demasked())
