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
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.errors import (
    ForbiddenError,
    ProcessError,
    TooManyRequestsError,
    ValidationError,
)
from app.metrics import (
    EventLoopDelaySampler,
    ProcessMetrics,
    StoreEvictionTask,
    render_prometheus,
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
from app.policy import PolicyRegistry
from app.process_service import ProcessService
from app.process_store import ProcessStore
from app.routing import RoundRobinRouter
from app.upstream import UpstreamClient

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


def _upstream_clients(
    client: httpx.AsyncClient,
) -> list[UpstreamClient]:
    """Build upstream clients: mock backends plus an optional real model.

    ``BACKEND_URLS`` are mock backends (text/plain). ``UPSTREAM_URL`` is an
    optional real OpenAI-compatible model (SSE). When set, it is appended to
    the rotation so a demo can compare mock vs real responses.
    """
    clients: list[UpstreamClient] = []
    for url in _backend_urls():
        clients.append(UpstreamClient(base_url=url, client=client))
    real_url = os.getenv("UPSTREAM_URL")
    if real_url:
        clients.append(
            UpstreamClient(
                base_url=real_url,
                client=client,
                api_key=os.getenv("UPSTREAM_API_KEY"),
                model=os.getenv("UPSTREAM_MODEL"),
            )
        )
    return clients


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.casefold() in {"1", "true", "yes", "on"}


def _extract_api_key(request: Request) -> str | None:
    """Extract the API key from Authorization Bearer or X-API-Key header."""
    auth = request.headers.get("Authorization")
    if auth and auth.casefold().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("X-API-Key")


def _mask_payload(
    body: ChatCompletionRequest,
    detectors: Sequence[PIIDetector],
    *,
    enabled_pii_types: frozenset[str] | None = None,
    masking_mode: str = "typed_placeholder",
    ml_detectors: Sequence[PIIDetector] | None = None,
    degradation: str = "fail_closed",
) -> tuple[dict[str, object], PIIMasker]:
    masker = PIIMasker(
        detectors=detectors,
        enabled_pii_types=enabled_pii_types,
        masking_mode=masking_mode,
        ml_detectors=ml_detectors,
        degradation=degradation,
    )
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
    app.state.upstream_clients = _upstream_clients(app.state.http_client)
    app.state.router = RoundRobinRouter(
        [f"upstream-{i}" for i in range(len(app.state.upstream_clients))]
    )
    app.state.ner_enabled = _env_enabled("NER_ENABLED")
    app.state.ner_semaphore = asyncio.Semaphore(
        int(os.getenv("NER_MAX_CONCURRENCY", "1"))
    )
    detectors = list(default_rule_detectors())
    ml_detectors: list[PIIDetector] = []
    if app.state.ner_enabled:
        model_name = os.getenv("NER_MODEL", "LLAIMlegal/ru-legal-ner")
        model_revision = os.getenv("NER_MODEL_REVISION", DEFAULT_NER_MODEL_REVISION)
        device = os.getenv("NER_DEVICE", "cpu")
        model_path = os.getenv("NER_MODEL_PATH")
        started_at = time.perf_counter()
        try:
            ner_backend = await asyncio.to_thread(
                TransformersNERBackend.from_pretrained,
                model_name,
                revision=model_revision,
                device=device,
                max_length=int(os.getenv("NER_MAX_LENGTH", "512")),
                stride=int(os.getenv("NER_STRIDE", "64")),
                offline=_env_enabled("NER_OFFLINE", default=True),
                model_path=model_path,
            )
        except Exception as exc:
            logger.error(
                "ner_model_initialization_failed model=%s model_path=%s error_type=%s",
                model_name,
                model_path or "default",
                type(exc).__name__,
            )
            raise
        ml_detectors.append(
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
    app.state.ml_detectors = tuple(ml_detectors)
    app.state.process_engine = PIIMaskingEngine(
        app.state.pii_detectors,
        max_workers=int(os.getenv("PROCESS_MASK_WORKERS", "64")),
        ml_detectors=app.state.ml_detectors,
    )
    app.state.process_metrics = ProcessMetrics()
    app.state.event_loop_sampler = EventLoopDelaySampler(
        interval=float(os.getenv("METRICS_EVENT_LOOP_INTERVAL_SECONDS", "1.0"))
    )
    app.state.event_loop_sampler.start()
    app.state.metrics_started_at = time.monotonic()
    store = ProcessStore(
        time_func=time.monotonic,
        active_ttl=float(os.getenv("PROCESS_ACTIVE_TTL_SECONDS", "900")),
        completed_ttl=float(os.getenv("PROCESS_COMPLETED_TTL_SECONDS", "120")),
        max_entries=int(os.getenv("PROCESS_STORE_MAX_ENTRIES", "200000")),
        max_bytes=int(os.getenv("PROCESS_STORE_MAX_BYTES", "536870912")),
        tombstone_ttl=float(os.getenv("PROCESS_TOMBSTONE_TTL_SECONDS", "60")),
        tombstone_max_entries=int(
            os.getenv("PROCESS_TOMBSTONE_MAX_ENTRIES", "100000")
        ),
    )
    app.state.store_eviction_task = StoreEvictionTask(
        store,
        app.state.process_metrics,
        interval=float(os.getenv("STORE_EVICTION_INTERVAL_SECONDS", "1.0")),
        batch_limit=int(os.getenv("STORE_EVICTION_BATCH_LIMIT", "1000")),
    )
    app.state.store_eviction_task.start()
    app.state.process_service = ProcessService(
        engine=app.state.process_engine,
        store=store,
        waiter_timeout=float(os.getenv("PROCESS_WAITER_TIMEOUT_SECONDS", "5")),
        max_payload_bytes=int(os.getenv("PROCESS_MAX_PAYLOAD_BYTES", "400000")),
        max_estimated_tokens=int(os.getenv("PROCESS_MAX_ESTIMATED_TOKENS", "100000")),
        metrics=app.state.process_metrics,
    )
    app.state.policy_registry = PolicyRegistry(
        config_path=os.getenv("POLICY_CONFIG_PATH"),
        reload_interval=float(os.getenv("POLICY_RELOAD_INTERVAL_SECONDS", "30")),
    )
    yield
    app.state.process_engine.close()
    await app.state.store_eviction_task.stop()
    await app.state.event_loop_sampler.stop()
    await app.state.http_client.aclose()


app = FastAPI(title="PrivyGate", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    service = getattr(request.app.state, "process_service", None)
    metrics_obj = getattr(request.app.state, "process_metrics", None)
    sampler = getattr(request.app.state, "event_loop_sampler", None)
    started_at = getattr(request.app.state, "metrics_started_at", time.monotonic())
    if service is None or metrics_obj is None or sampler is None:
        return Response(
            content="metrics not initialized\n",
            status_code=503,
            media_type="text/plain",
        )
    body = render_prometheus(
        service.store,
        metrics_obj,
        sampler.delay_ms,
        started_at=started_at,
    )
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4",
    )


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


@app.exception_handler(ForbiddenError)
async def forbidden_error_handler(request: Request, exc: ForbiddenError):
    return JSONResponse(status_code=403, content={"detail": exc.message})


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
    registry = getattr(request.app.state, "policy_registry", None)
    consumer_id = request.headers.get("X-Consumer-ID")
    api_key = _extract_api_key(request)
    if registry is not None and not registry.is_allowed(consumer_id, api_key):
        raise ForbiddenError("consumer not allowed")
    policy = registry.resolve(consumer_id) if registry is not None else None

    detectors = getattr(request.app.state, "pii_detectors", None)
    if detectors is None:
        detectors = default_rule_detectors()
    ml_detectors = getattr(request.app.state, "ml_detectors", None)
    mask_kwargs = (
        {
            "enabled_pii_types": policy.effective_pii_types,
            "masking_mode": policy.masking_mode,
            "ml_detectors": ml_detectors,
            "degradation": policy.degradation,
        }
        if policy is not None
        else {"ml_detectors": ml_detectors}
    )
    if getattr(request.app.state, "ner_enabled", False):
        async with request.app.state.ner_semaphore:
            payload, masker = await asyncio.to_thread(
                _mask_payload, body, detectors, **mask_kwargs
            )
    else:
        payload, masker = _mask_payload(body, detectors, **mask_kwargs)

    backend_index = int((await request.app.state.router.next_backend()).split("-")[1])
    upstream_client = request.app.state.upstream_clients[backend_index]
    pii_decisions = ",".join(
        f"{decision.pii_type}:{decision.confidence:.2f}:{decision.action}"
        for decision in masker.decisions
    ) or "none"
    logger.info(
        "request_started request_id=%s backend=%s pii_count=%d pii_types=%s "
        "pii_candidates_count=%d pii_decisions=%s",
        request_id,
        backend_index,
        len(masker.mapping),
        ",".join(masker.pii_types) or "none",
        len(masker.decisions),
        pii_decisions,
    )

    try:
        upstream = await upstream_client.stream_chat(
            payload=payload,
            mapping=masker.mapping,
            request_id=request_id,
            pii_types=masker.pii_types,
            allow_demasking=(
                policy.allow_demasking if policy is not None else True
            ),
        )
    except (httpx.HTTPError, OSError) as exc:
        # Log only the exception class: an exception message can contain unsafe data.
        logger.error(
            "upstream_unavailable request_id=%s backend=%s error_type=%s",
            request_id,
            backend_index,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="LLM backend unavailable") from None

    media_type = upstream.response.headers.get("content-type", "text/plain").split(";", 1)[0]
    return StreamingResponse(
        upstream.body,
        status_code=upstream.response.status_code,
        media_type=media_type,
        headers={"X-Request-ID": request_id, "X-Backend": str(backend_index)},
    )
