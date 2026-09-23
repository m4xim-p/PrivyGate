"""OpenAI-compatible upstream LLM client with streaming and SSE demasking.

Supports two upstream formats:
- ``text/plain`` (mock backends): demasks the raw text stream.
- ``text/event-stream`` (real OpenAI-compatible models): parses SSE events,
  demasks ``choices[0].delta.content``, and re-emits SSE.

The client is an upstream adapter: it knows nothing about FastAPI, PII
detection, or the /process contract. Masking/demasking is delegated to
``StreamingDemasker``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.pii import StreamingDemasker

logger = logging.getLogger("privygate.upstream")


@dataclass(slots=True)
class UpstreamStream:
    response: httpx.Response
    body: AsyncIterator[str]


class UpstreamClient:
    """Streaming client for an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        model: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._model = model

    def _headers(self, request_id: str, api_key: str | None) -> dict[str, str]:
        headers = {"X-Request-ID": request_id}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def stream_chat(
        self,
        *,
        payload: dict[str, object],
        mapping: dict[str, str],
        request_id: str,
        pii_types: list[str],
        allow_demasking: bool = True,
        api_key: str | None = None,
    ) -> UpstreamStream:
        started_at = time.perf_counter()
        if self._model:
            payload = {**payload, "model": self._model}
        request = self._client.build_request(
            "POST",
            f"{self._base_url}/v1/chat/completions",
            json=payload,
            headers=self._headers(request_id, api_key),
        )
        response = await self._client.send(request, stream=True)

        if response.is_error:
            status_code = response.status_code
            await response.aclose()
            logger.warning(
                "upstream_rejected request_id=%s status=%d pii_count=%d pii_types=%s",
                request_id,
                status_code,
                len(mapping),
                ",".join(pii_types) or "none",
            )
            raise httpx.HTTPStatusError(
                "Upstream returned an error",
                request=request,
                response=response,
            )

        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            body = self._iter_sse_demasked(
                response, mapping, allow_demasking, request_id, started_at, pii_types
            )
        else:
            body = self._iter_text_demasked(
                response, mapping, allow_demasking, request_id, started_at, pii_types
            )

        return UpstreamStream(response=response, body=body)

    async def _iter_text_demasked(
        self,
        response: httpx.Response,
        mapping: dict[str, str],
        allow_demasking: bool,
        request_id: str,
        started_at: float,
        pii_types: list[str],
    ) -> AsyncIterator[str]:
        demasker = StreamingDemasker(mapping) if allow_demasking else None
        status = "completed"
        try:
            async for chunk in response.aiter_text():
                if demasker is None:
                    yield chunk
                    continue
                restored = demasker.feed(chunk)
                if restored:
                    yield restored
            if demasker is not None:
                tail = demasker.flush()
                if tail:
                    yield tail
        except Exception:
            status = "stream_error"
            raise
        finally:
            await response.aclose()
            self._log_finished(request_id, status, started_at, mapping, pii_types)

    async def _iter_sse_demasked(
        self,
        response: httpx.Response,
        mapping: dict[str, str],
        allow_demasking: bool,
        request_id: str,
        started_at: float,
        pii_types: list[str],
    ) -> AsyncIterator[str]:
        demasker = StreamingDemasker(mapping) if allow_demasking else None
        status = "completed"
        try:
            async for line in response.aiter_lines():
                emitted = self._process_sse_line(line, demasker)
                if emitted:
                    yield emitted
            if demasker is not None:
                tail = demasker.flush()
                if tail:
                    event = {"choices": [{"index": 0, "delta": {"content": tail}}]}
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        except Exception:
            status = "stream_error"
            raise
        finally:
            await response.aclose()
            self._log_finished(request_id, status, started_at, mapping, pii_types)

    def _process_sse_line(
        self,
        line: str,
        demasker: StreamingDemasker | None,
    ) -> str | None:
        """Process one SSE line, returning the (possibly demasked) output or None."""
        if not line.startswith("data:"):
            return line + "\n"
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return "data: [DONE]\n\n"
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return line + "\n"
        delta = self._extract_delta(event)
        if delta is None:
            return line + "\n"
        if demasker is None:
            return line + "\n"
        restored = demasker.feed(delta)
        if not restored:
            # Demasker buffered (placeholder split across events); emit nothing.
            return None
        self._set_delta(event, restored)
        return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    @staticmethod
    def _extract_delta(event: dict) -> str | None:
        try:
            choices = event.get("choices") or []
            if not choices:
                return None
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            return content if isinstance(content, str) else None
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _set_delta(event: dict, content: str) -> None:
        try:
            choices = event.get("choices") or []
            if choices:
                delta = choices[0].setdefault("delta", {})
                delta["content"] = content
        except (KeyError, IndexError, TypeError):
            pass

    @staticmethod
    def _log_finished(
        request_id: str,
        status: str,
        started_at: float,
        mapping: dict[str, str],
        pii_types: list[str],
    ) -> None:
        latency_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "request_finished request_id=%s status=%s latency_ms=%.1f "
            "pii_count=%d pii_types=%s",
            request_id,
            status,
            latency_ms,
            len(mapping),
            ",".join(pii_types) or "none",
        )
