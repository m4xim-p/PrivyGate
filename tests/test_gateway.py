import asyncio
import json

import httpx

from app.main import app
from app.routing import RoundRobinRouter


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
        app.state.router = RoundRobinRouter(["http://mock-backend"])

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
