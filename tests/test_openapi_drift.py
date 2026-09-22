"""OpenAPI drift test: keep process_api.yaml, FastAPI schema and models in sync."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.main import app
from app.models import ProcessRequest, ProcessResponse

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "process_api.yaml"


def _load_spec() -> dict:
    with SPEC_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_spec_file_exists_and_is_valid() -> None:
    spec = _load_spec()
    assert spec["openapi"] == "3.0.3"
    assert "/process" in spec["paths"]


def test_fastapi_exposes_process_endpoint() -> None:
    schema = app.openapi()
    assert "/process" in schema["paths"]
    post = schema["paths"]["/process"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessRequest"
    }
    assert post["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessResponse"
    }


def test_models_match_spec_schema() -> None:
    spec = _load_spec()
    schemas = spec["components"]["schemas"]

    req = schemas["ProcessRequest"]
    assert set(req["required"]) == {"payload", "payload_id"}
    assert req["properties"]["payload"]["type"] == "string"
    assert req["properties"]["payload_id"]["type"] == "string"

    resp = schemas["ProcessResponse"]
    assert resp["required"] == ["result"]
    assert resp["properties"]["result"]["type"] == "string"


def test_models_match_fastapi_schema() -> None:
    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    req = schemas["ProcessRequest"]
    assert set(req["required"]) == {"payload", "payload_id"}
    assert req["properties"]["payload"]["type"] == "string"
    assert req["properties"]["payload_id"]["type"] == "string"

    resp = schemas["ProcessResponse"]
    assert resp["required"] == ["result"]
    assert resp["properties"]["result"]["type"] == "string"


def test_spec_and_fastapi_agree_on_request_fields() -> None:
    spec = _load_spec()["components"]["schemas"]["ProcessRequest"]
    fastapi = app.openapi()["components"]["schemas"]["ProcessRequest"]

    assert set(spec["required"]) == set(fastapi["required"])
    assert set(spec["properties"]) == set(fastapi["properties"])


def test_pydantic_models_are_consistent() -> None:
    # The pydantic models must expose exactly the fields the spec declares.
    req_fields = set(ProcessRequest.model_fields)
    resp_fields = set(ProcessResponse.model_fields)
    assert req_fields == {"payload", "payload_id"}
    assert resp_fields == {"result"}
