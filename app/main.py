"""FastAPI entrypoint for the privacy gateway."""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app.errors import (
    ProcessError,
    TooManyRequestsError,
    ValidationError,
)
from app.models import ChatCompletionRequest, ProcessRequest, ProcessResponse
from app.ner import DEFAULT_NER_MODEL_REVISION, NERDetector, TransformersNERBackend
from app.pii import (
    NameDetector,
    PIIDetector,
    PIIMasker,
    default_rule_detectors,
)
from app.pii_engine import PIIMaskingEngine
from app.process_service import ProcessService
from app.process_store import ProcessStore
from app.proxy import open_upstream_stream
from app.routing import RoundRobinRouter

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("privygate.gateway")


def _backend_urls() -> list[str]:
    configured = os.getenv(
        "BACKEND_URLS",
        "http://localhost:8001,http://localhost:8002,http://localhost:8003",
    )
    return [url.strip() for url in configured.split(",") if url.strip()]


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.casefold() in {"1", "true", "yes", "on"}


def _mask_payload(
    body: ChatCompletionRequest,
    detectors: Sequence[PIIDetector],
) -> tuple[dict[str, object], PIIMasker]:
    masker = PIIMasker(detectors=detectors)
    payload = body.as_upstream_payload()
    for message in payload["messages"]:
        message["content"] = masker.mask(message["content"])
    return payload, masker


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)
    app.state.http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100),
    )
    app.state.router = RoundRobinRouter(_backend_urls())
    app.state.ner_enabled = _env_enabled("NER_ENABLED")
    app.state.ner_semaphore = asyncio.Semaphore(
        int(os.getenv("NER_MAX_CONCURRENCY", "1"))
    )
    detectors = list(default_rule_detectors())
    if app.state.ner_enabled:
        model_name = os.getenv("NER_MODEL", "LLAIMlegal/ru-legal-ner")
        model_revision = os.getenv("NER_MODEL_REVISION", DEFAULT_NER_MODEL_REVISION)
        device = os.getenv("NER_DEVICE", "cpu")
        started_at = time.perf_counter()
        try:
            ner_backend = await asyncio.to_thread(
                TransformersNERBackend.from_pretrained,
                model_name,
                revision=model_revision,
                device=device,
                max_length=int(os.getenv("NER_MAX_LENGTH", "512")),
                stride=int(os.getenv("NER_STRIDE", "64")),
            )
        except Exception as exc:
            logger.error(
                "ner_model_initialization_failed model=%s error_type=%s",
                model_name,
                type(exc).__name__,
            )
            raise
        detectors.append(
            NERDetector(
                ner_backend,
                min_confidence=float(os.getenv("NER_MIN_CONFIDENCE", "0.80")),
                precheck=NameDetector(),
            )
        )
        logger.info(
            "ner_model_initialized model=%s latency_ms=%.1f device=%s",
            model_name,
            (time.perf_counter() - started_at) * 1000,
            device,
        )
    app.state.pii_detectors = tuple(detectors)
    app.state.process_engine = PIIMaskingEngine(
        app.state.pii_detectors,
        max_workers=int(os.getenv("PROCESS_MASK_WORKERS", "64")),
    )
    app.state.process_service = ProcessService(
        engine=app.state.process_engine,
        store=ProcessStore(
            time_func=time.monotonic,
            active_ttl=float(os.getenv("PROCESS_ACTIVE_TTL_SECONDS", "900")),
            completed_ttl=float(os.getenv("PROCESS_COMPLETED_TTL_SECONDS", "120")),
            max_entries=int(os.getenv("PROCESS_STORE_MAX_ENTRIES", "25000")),
            max_bytes=int(os.getenv("PROCESS_STORE_MAX_BYTES", "536870912")),
        ),
        waiter_timeout=float(os.getenv("PROCESS_WAITER_TIMEOUT_SECONDS", "5")),
        max_payload_bytes=int(os.getenv("PROCESS_MAX_PAYLOAD_BYTES", "400000")),
        max_estimated_tokens=int(os.getenv("PROCESS_MAX_ESTIMATED_TOKENS", "100000")),
    )
    yield
    app.state.process_engine.close()
    await app.state.http_client.aclose()


app = FastAPI(title="PrivyGate", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}


@app.exception_handler(ProcessError)
async def process_error_handler(request: Request, exc: ProcessError):
    headers: dict[str, str] = {}
    if isinstance(exc, TooManyRequestsError):
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Never echo the offending input back: it may contain raw PII.
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": errors},
    )


@app.post(
    "/process",
    responses={
        200: {"model": ProcessResponse, "description": "Успешная обработка"},
        409: {"description": "Существующий payload_id использован с неизвестным payload"},
        413: {"description": "Payload превышает допустимый размер"},
        422: {"description": "Отсутствует поле или нарушен тип schema"},
        429: {"description": "Слишком много запросов (admission limit)"},
        410: {"description": "payload_id истёк (tombstone)"},
        500: {"description": "Внутренняя ошибка сервиса"},
    },
)
async def process(body: ProcessRequest, request: Request) -> ProcessResponse:
    service = getattr(request.app.state, "process_service", None)
    if service is None:
        raise ValidationError("process service not initialized")
    result = await service.process(body.payload, body.payload_id)
    return ProcessResponse(result=result)


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
) -> StreamingResponse:
    request_id = str(uuid.uuid4())
    detectors = getattr(request.app.state, "pii_detectors", None)
    if detectors is None:
        detectors = default_rule_detectors()
    if getattr(request.app.state, "ner_enabled", False):
        async with request.app.state.ner_semaphore:
            payload, masker = await asyncio.to_thread(_mask_payload, body, detectors)
    else:
        payload, masker = _mask_payload(body, detectors)

    backend = await request.app.state.router.next_backend()
    pii_decisions = ",".join(
        f"{decision.pii_type}:{decision.confidence:.2f}:{decision.action}"
        for decision in masker.decisions
    ) or "none"
    logger.info(
        "request_started request_id=%s backend=%s pii_count=%d pii_types=%s "
        "pii_candidates_count=%d pii_decisions=%s",
        request_id,
        backend,
        len(masker.mapping),
        ",".join(masker.pii_types) or "none",
        len(masker.decisions),
        pii_decisions,
    )

    try:
        upstream = await open_upstream_stream(
            client=request.app.state.http_client,
            backend=backend,
            payload=payload,
            mapping=masker.mapping,
            request_id=request_id,
            pii_types=masker.pii_types,
        )
    except (httpx.HTTPError, OSError) as exc:
        # Log only the exception class: an exception message can contain unsafe data.
        logger.error(
            "upstream_unavailable request_id=%s backend=%s error_type=%s",
            request_id,
            backend,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="LLM backend unavailable") from None

    media_type = upstream.response.headers.get("content-type", "text/plain").split(";", 1)[0]
    return StreamingResponse(
        upstream.body,
        status_code=upstream.response.status_code,
        media_type=media_type,
        headers={"X-Request-ID": request_id, "X-Backend": backend},
    )
