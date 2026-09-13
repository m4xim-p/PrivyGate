"""Configurable mock backend used as three independent service instances."""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from app.models import ChatCompletionRequest
from app.pii import (
    DEFAULT_MASKING_CONFIDENCE,
    EmailDetector,
    PhoneDetector,
)


BACKEND_ID = os.getenv("BACKEND_ID", "backend-1")
CHUNK_DELAY_SECONDS = float(os.getenv("CHUNK_DELAY_SECONDS", "0.2"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "12"))
PII_PLACEHOLDER_PATTERN = re.compile(r"__PII_[A-Z]+_\d+__")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("privygate.mock_llm")
email_detector = EmailDetector()
phone_detector = PhoneDetector()


@dataclass(frozen=True, slots=True)
class RequestDiagnostics:
    pii_placeholder_count: int
    pii_placeholders: tuple[str, ...]
    raw_email_detected: bool
    raw_phone_detected: bool


def inspect_messages(contents: list[str]) -> RequestDiagnostics:
    """Return only aggregate, non-sensitive facts safe enough for diagnostics."""

    placeholders = tuple(
        placeholder
        for content in contents
        for placeholder in PII_PLACEHOLDER_PATTERN.findall(content)
    )
    return RequestDiagnostics(
        pii_placeholder_count=len(placeholders),
        pii_placeholders=placeholders,
        raw_email_detected=any(
            match.confidence >= DEFAULT_MASKING_CONFIDENCE
            for content in contents
            for match in email_detector.detect(content)
        ),
        raw_phone_detected=any(
            match.confidence >= DEFAULT_MASKING_CONFIDENCE
            for content in contents
            for match in phone_detector.detect(content)
        ),
    )


app = FastAPI(title=f"Mock LLM {BACKEND_ID}", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": BACKEND_ID}


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest):
    diagnostics = inspect_messages([message.content for message in body.messages])
    logger.info(
        "request_received backend_id=%s pii_placeholder_count=%d "
        "pii_placeholders=%s raw_email_detected=%s raw_phone_detected=%s "
        "response_chunk_size=%d",
        BACKEND_ID,
        diagnostics.pii_placeholder_count,
        ",".join(diagnostics.pii_placeholders) or "none",
        str(diagnostics.raw_email_detected).lower(),
        str(diagnostics.raw_phone_detected).lower(),
        CHUNK_SIZE,
    )

    user_messages = [message.content for message in body.messages if message.role == "user"]
    prompt = user_messages[-1] if user_messages else ""
    answer = f"Ответ {BACKEND_ID}: Получен запрос: {prompt}"

    if not body.stream:
        return JSONResponse(
            {
                "id": f"mock-{BACKEND_ID}",
                "object": "chat.completion",
                "model": body.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}}],
            }
        )

    async def generate() -> AsyncIterator[bytes]:
        # Byte chunks also exercise httpx's incremental UTF-8 decoder.
        encoded = answer.encode("utf-8")
        offsets = range(0, len(encoded), CHUNK_SIZE)
        for offset in offsets:
            yield encoded[offset : offset + CHUNK_SIZE]
            if offset + CHUNK_SIZE < len(encoded):
                await asyncio.sleep(CHUNK_DELAY_SECONDS)

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")
