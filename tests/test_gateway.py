import asyncio
import json

import httpx

from app.main import app
from app.model_registry import ModelRegistry


def _test_registry() -> ModelRegistry:
    """A registry with a single model pointing at the mock backend."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False
    ) as f:
        json.dump(
            {
                "models": [
                    {
                        "name": "mock-model",
                        "api_base": "http://mock-backend",
                        "model": "mock-model",
                    }
                ]
            },
            f,
        )
        path = f.name
    return ModelRegistry(config_path=path, reload_interval=0)


def test_gateway_sends_only_masked_pii_to_backend() -> None:
    raw_email = "private.user@example.org"
    raw_phone = "+7 999 111-22-33"
    captured_payload: dict[str, object] = {}

    def backend_handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        content = captured_payload["messages"][-1]["content"]
        return httpx.Response(200, text=f"backend received: {content}")

    async def exercise_gateway() -> httpx.Response:
        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(backend_handler)
        )
        app.state.http_client = upstream_client
        app.state.model_registry = _test_registry()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                return await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "mock-model",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"Позвони {raw_phone} или напиши {raw_email}",
                            }
                        ],
                        "stream": True,
                    },
                )
        finally:
            await upstream_client.aclose()

    response = asyncio.run(exercise_gateway())
    upstream_content = captured_payload["messages"][-1]["content"]

    assert response.status_code == 200
    assert raw_email not in upstream_content
    assert raw_phone not in upstream_content
    assert "__PII_EMAIL_1__" in upstream_content
    assert "__PII_PHONE_1__" in upstream_content
    # The client still receives its original values after streaming demasking.
    assert raw_email in response.text
    assert raw_phone in response.text


def test_gateway_sse_demasking_real_model() -> None:
    """SSE responses from a real model are demasked in delta.content."""
    raw_email = "private.user@example.org"
    captured_payload: dict[str, object] = {}

    def backend_handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        content = captured_payload["messages"][-1]["content"]
        # Simulate an OpenAI-compatible SSE stream that echoes the masked content.
        sse = (
            "data: {\"choices\":[{\"delta\":{\"content\":\"Ответ: \"}}]}\n\n"
            f"data: {{\"choices\":[{{\"delta\":{{\"content\":\"{content}\"}}}}]}}\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            text=sse,
            headers={"content-type": "text/event-stream"},
        )

    async def exercise_gateway() -> httpx.Response:
        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(backend_handler)
        )
        app.state.http_client = upstream_client
        app.state.model_registry = _test_registry()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                return await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "mock-model",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"Напиши на {raw_email}",
                            }
                        ],
                        "stream": True,
                    },
                )
        finally:
            await upstream_client.aclose()

    response = asyncio.run(exercise_gateway())
    upstream_content = captured_payload["messages"][-1]["content"]

    # Upstream received masked content.
    assert raw_email not in upstream_content
    assert "__PII_EMAIL_1__" in upstream_content
    # Client receives the original email after SSE demasking.
    assert raw_email in response.text
    # SSE structure preserved.
    assert "data:" in response.text
    assert "[DONE]" in response.text


def test_gateway_unknown_model_returns_404() -> None:
    """Requesting an unknown model returns 404."""

    async def exercise_gateway() -> httpx.Response:
        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
        )
        app.state.http_client = upstream_client
        app.state.model_registry = _test_registry()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                return await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "does-not-exist",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                )
        finally:
            await upstream_client.aclose()

    response = asyncio.run(exercise_gateway())
    assert response.status_code == 404


def test_gateway_forwards_model_api_key() -> None:
    """The client's X-Model-API-Key is forwarded to the upstream."""
    captured_headers: dict[str, str] = {}

    def backend_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, text="ok")

    async def exercise_gateway() -> httpx.Response:
        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(backend_handler)
        )
        app.state.http_client = upstream_client
        app.state.model_registry = _test_registry()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                return await client.post(
                    "/v1/chat/completions",
                    headers={"X-Model-API-Key": "client-secret-key"},
                    json={
                        "model": "mock-model",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                )
        finally:
            await upstream_client.aclose()

    response = asyncio.run(exercise_gateway())
    assert response.status_code == 200
    assert captured_headers.get("authorization") == "Bearer client-secret-key"


def test_gateway_token_quota_exceeded_returns_429() -> None:
    """Requesting more tokens than the consumer quota returns 429."""
    import tempfile

    from app.policy import PolicyRegistry

    def backend_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async def exercise_gateway() -> httpx.Response:
        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(backend_handler)
        )
        app.state.http_client = upstream_client
        app.state.model_registry = _test_registry()

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "consumers": [
                        {
                            "consumer_id": "quota-agent",
                            "max_tokens_per_request": 10,
                            "api_keys": ["quota-key"],
                        }
                    ]
                },
                f,
            )
            policy_path = f.name
        app.state.policy_registry = PolicyRegistry(
            config_path=policy_path, reload_interval=0
        )

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                return await client.post(
                    "/v1/chat/completions",
                    headers={
                        "X-Consumer-ID": "quota-agent",
                        "Authorization": "Bearer quota-key",
                    },
                    json={
                        "model": "mock-model",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                        "max_tokens": 100,
                    },
                )
        finally:
            await upstream_client.aclose()

    response = asyncio.run(exercise_gateway())
    assert response.status_code == 429
